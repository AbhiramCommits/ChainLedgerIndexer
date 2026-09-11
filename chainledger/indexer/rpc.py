import asyncio
import itertools
import json
import threading
from collections.abc import Callable, Collection, Coroutine
from typing import Any, TypeVar, cast

import requests
import structlog
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from web3 import Web3
from web3.exceptions import RequestTimedOut, Web3Exception, Web3RPCError
from web3.providers import BaseProvider, HTTPProvider
from web3.types import RPCEndpoint, RPCResponse
from websockets.asyncio.client import connect as ws_connect
from websockets.protocol import State

logger = structlog.get_logger()

MAX_ATTEMPTS = 5


def is_retryable(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            # Non-2xx responses (429 rate limits, 5xx) surface as HTTPError
            # because the provider calls raise_for_status().
            requests.exceptions.HTTPError,
        ),
    ):
        return True
    if isinstance(exc, RequestTimedOut):
        return True
    if isinstance(exc, Web3Exception):
        # Reverts (ContractLogicError) and deterministic JSON-RPC errors (e.g.
        # "query returned more than N results", handled via range bisection)
        # cannot succeed on retry.
        return False
    if isinstance(exc, OSError):
        return True
    msg = str(exc).lower()
    return any(token in msg for token in ("429", "rate limit", "timed out", "connection closed"))


def _log_retry(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait = getattr(retry_state.next_action, "sleep", None)
    logger.warning(
        "rpc call failed, retrying",
        attempt=retry_state.attempt_number,
        wait_seconds=wait,
        error=str(exc),
    )


def _retry_kwargs() -> dict[str, Any]:
    return {
        "retry": retry_if_exception(is_retryable),
        "stop": stop_after_attempt(MAX_ATTEMPTS),
        "wait": wait_exponential(multiplier=1, min=1, max=30),
        "before_sleep": _log_retry,
        "reraise": True,
    }


T = TypeVar("T")


def _with_retry(fn: Callable[[], T]) -> T:
    for attempt in Retrying(**_retry_kwargs()):
        with attempt:
            return fn()
    raise AssertionError("unreachable")


class RetryingHTTPProvider(HTTPProvider):
    """HTTP provider that retries transport-level failures with tenacity."""

    def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        make = super().make_request
        return _with_retry(lambda: make(method, params))

    def make_batch_request(
        self, batch_requests: list[tuple[RPCEndpoint, Any]]
    ) -> list[RPCResponse] | RPCResponse:
        make_batch = super().make_batch_request
        return _with_retry(lambda: make_batch(batch_requests))


class RetryingWebSocketProvider(BaseProvider):
    """Synchronous websocket JSON-RPC provider.

    web3 8 removed its synchronous websocket provider: the only bundled ws
    transport is the async persistent-connection provider, which sync ``Web3``
    refuses to use. This class implements the sync ``BaseProvider`` protocol
    directly on top of the ``websockets`` library. A dedicated event-loop
    thread owns the socket; ``make_request``/``make_batch_request`` bridge
    into it and block for the correlated JSON-RPC response, with tenacity
    retry and reconnection on transport failures.
    """

    def __init__(self, endpoint_uri: str, request_timeout: float = 30.0) -> None:
        super().__init__()
        self.endpoint_uri = endpoint_uri
        self.request_timeout = request_timeout
        self._ids = itertools.count(1)
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="chainledger-ws", daemon=True
        )
        self._thread.start()
        self.connect_sync()

    def _await(self, coro: Coroutine[Any, Any, T]) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def connect_sync(self) -> None:
        def _connect() -> None:
            if self._ws is not None:
                return

            async def _open() -> Any:
                return await ws_connect(self.endpoint_uri)

            self._ws = self._await(_open())

        _with_retry(_connect)

    def _ensure_connected(self) -> None:
        if self._ws is None or self._ws.state is not State.OPEN:
            self._ws = None
            self.connect_sync()

    def _next_id(self) -> int:
        return next(self._ids)

    async def _recv_for_id(self, request_id: int) -> RPCResponse:
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.request_timeout)
            resp = json.loads(raw)
            if isinstance(resp, list):
                for entry in resp:
                    if entry.get("id") == request_id:
                        return cast(RPCResponse, entry)
            elif resp.get("id") == request_id:
                return cast(RPCResponse, resp)

    def make_request(self, method: RPCEndpoint, params: Any) -> RPCResponse:
        def _request() -> RPCResponse:
            self._ensure_connected()
            request_id = self._next_id()
            payload = json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id}
            )

            async def _do() -> RPCResponse:
                assert self._ws is not None
                await self._ws.send(payload)
                return await self._recv_for_id(request_id)

            return self._await(_do())

        return _with_retry(_request)

    def make_batch_request(
        self, batch_requests: list[tuple[RPCEndpoint, Any]]
    ) -> list[RPCResponse] | RPCResponse:
        def _request() -> list[RPCResponse]:
            self._ensure_connected()
            request_ids = [self._next_id() for _ in batch_requests]
            payload = json.dumps(
                [
                    {"jsonrpc": "2.0", "method": method, "params": params, "id": rid}
                    for (method, params), rid in zip(batch_requests, request_ids, strict=True)
                ]
            )

            async def _do() -> list[RPCResponse]:
                assert self._ws is not None
                await self._ws.send(payload)
                pending = dict.fromkeys(request_ids)
                results: dict[int, RPCResponse] = {}
                while pending:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=self.request_timeout)
                    entries = json.loads(raw)
                    if not isinstance(entries, list):
                        entries = [entries]
                    for entry in entries:
                        rid = entry.get("id")
                        if rid in pending:
                            results[rid] = cast(RPCResponse, entry)
                            del pending[rid]
                return [results[rid] for rid in request_ids]

            return self._await(_do())

        return _with_retry(_request)

    def is_connected(self, show_traceback: bool = False) -> bool:
        return self._ws is not None and self._ws.state is State.OPEN

    def close(self) -> None:
        """Close the socket and shut down the event-loop thread."""

        async def _close() -> None:
            if self._ws is not None:
                await self._ws.close()
                self._ws = None

        try:
            self._await(_close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


def make_client(rpc_url: str) -> Web3:
    if rpc_url.startswith(("http://", "https://")):
        return Web3(RetryingHTTPProvider(rpc_url))
    if rpc_url.startswith(("ws://", "wss://")):
        return Web3(RetryingWebSocketProvider(rpc_url))
    raise ValueError(f"unsupported RPC_URL scheme: {rpc_url}")


def batch_get_block_timestamps(w3: Web3, block_numbers: Collection[int]) -> dict[int, int]:
    """Fetch block timestamps in a single JSON-RPC batch call."""
    if not block_numbers:
        return {}
    requests = [(RPCEndpoint("eth_getBlockByNumber"), [hex(n), False]) for n in block_numbers]
    provider = cast(Any, w3.provider)
    raw = provider.make_batch_request(requests)
    if isinstance(raw, dict):
        raise Web3RPCError(f"batch request failed: {raw}")
    timestamps: dict[int, int] = {}
    for resp in raw:
        if "error" in resp:
            raise Web3RPCError(f"eth_getBlockByNumber failed: {resp['error']}")
        result = resp["result"]
        number = int(result["number"], 16)
        timestamps[number] = int(result["timestamp"], 16)
    return timestamps
