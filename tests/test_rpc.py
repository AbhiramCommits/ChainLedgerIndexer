import asyncio
import json
import socket
import threading

import pytest
import requests
from tenacity import wait_none
from web3.exceptions import ContractLogicError, RequestTimedOut, Web3RPCError
from web3.providers import HTTPProvider
from web3.types import RPCEndpoint
from websockets.asyncio.server import serve

from chainledger.indexer import rpc as rpc_module
from chainledger.indexer.rpc import (
    RetryingHTTPProvider,
    RetryingWebSocketProvider,
    batch_get_block_timestamps,
    is_retryable,
    make_client,
)


def test_is_retryable_transport_errors():
    assert is_retryable(requests.exceptions.ConnectionError())
    assert is_retryable(requests.exceptions.Timeout())
    assert is_retryable(requests.exceptions.HTTPError("429 Too Many Requests"))
    assert is_retryable(OSError("connection reset by peer"))
    assert is_retryable(Exception("timed out"))


def test_is_not_retryable_rpc_errors():
    assert not is_retryable(Web3RPCError("query returned more than 10000 results"))
    assert not is_retryable(ContractLogicError("execution reverted"))
    assert not is_retryable(ValueError("boom"))


def test_is_retryable_timeouts():
    assert is_retryable(RequestTimedOut("timed out"))
    assert is_retryable(TimeoutError())


def test_make_client_http():
    w3 = make_client("http://localhost:8545")
    assert isinstance(w3.provider, RetryingHTTPProvider)


def test_make_client_ws(monkeypatch):
    monkeypatch.setattr(RetryingWebSocketProvider, "connect_sync", lambda self: None)
    w3 = make_client("ws://localhost:8546")
    assert isinstance(w3.provider, RetryingWebSocketProvider)


def test_make_client_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        make_client("unix:///tmp/geth.ipc")


class FakeProvider:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def make_batch_request(self, requests):
        self.calls += 1
        return [
            {"id": i, "result": {"number": hex(n), "timestamp": hex(self.responses[n])}}
            for i, (_, params) in enumerate(requests)
            for n in [int(params[0], 16)]
        ]


class FakeWeb3:
    def __init__(self, provider):
        self.provider = provider


def test_batch_get_block_timestamps():
    w3 = FakeWeb3(FakeProvider({1: 100, 2: 200}))
    assert batch_get_block_timestamps(w3, [1, 2]) == {1: 100, 2: 200}


def test_batch_get_block_timestamps_error():
    provider = FakeProvider({})
    provider.make_batch_request = lambda requests: {"error": {"code": -32000, "message": "boom"}}  # type: ignore[method-assign]
    with pytest.raises(Web3RPCError):
        batch_get_block_timestamps(FakeWeb3(provider), [1])


def test_batch_get_block_timestamps_entry_error():
    provider = FakeProvider({})

    def error_entry(requests):
        return [
            {"id": i, "error": {"code": -32000, "message": "boom"}} for i, _ in enumerate(requests)
        ]

    provider.make_batch_request = error_entry  # type: ignore[method-assign]
    with pytest.raises(Web3RPCError):
        batch_get_block_timestamps(FakeWeb3(provider), [1])


# --- retrying HTTP provider ---------------------------------------------------


def test_http_provider_retries_then_succeeds(monkeypatch):
    calls: list[int] = []

    def flaky(self, method, params):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("boom")
        return {"jsonrpc": "2.0", "id": 1, "result": "0x2a"}

    monkeypatch.setattr(HTTPProvider, "make_request", flaky)
    provider = RetryingHTTPProvider("http://localhost:1")
    resp = provider.make_request(RPCEndpoint("eth_blockNumber"), [])
    assert resp["result"] == "0x2a"
    assert len(calls) == 2


def test_http_provider_gives_up_after_max_attempts(monkeypatch):
    calls: list[int] = []

    def always_fails(self, method, params):
        calls.append(1)
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(HTTPProvider, "make_request", always_fails)
    monkeypatch.setattr(
        rpc_module,
        "_retry_kwargs",
        lambda: {
            "retry": rpc_module.retry_if_exception(rpc_module.is_retryable),
            "stop": rpc_module.stop_after_attempt(rpc_module.MAX_ATTEMPTS),
            "wait": wait_none(),
            "before_sleep": lambda state: None,
            "reraise": True,
        },
    )
    provider = RetryingHTTPProvider("http://localhost:1")
    with pytest.raises(requests.exceptions.Timeout):
        provider.make_request(RPCEndpoint("eth_blockNumber"), [])
    assert len(calls) == rpc_module.MAX_ATTEMPTS


def test_http_provider_retries_batch(monkeypatch):
    calls: list[int] = []

    def flaky_batch(self, batch_requests):
        calls.append(1)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("boom")
        return [
            {
                "jsonrpc": "2.0",
                "id": i,
                "result": {"number": hex(1), "timestamp": hex(2)},
            }
            for i, _ in enumerate(batch_requests)
        ]

    monkeypatch.setattr(HTTPProvider, "make_batch_request", flaky_batch)
    provider = RetryingHTTPProvider("http://localhost:1")
    resp = provider.make_batch_request([(RPCEndpoint("eth_getBlockByNumber"), ["0x1", False])])
    assert len(resp) == 1
    assert len(calls) == 2


# --- websocket sync bridge --------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ws_server():
    port = _free_port()
    state: dict = {}
    started = threading.Event()

    async def handler(ws):
        async for raw in ws:
            req = json.loads(raw)
            entries = req if isinstance(req, list) else [req]
            for entry in entries:
                method = entry["method"]
                if method == "eth_blockNumber":
                    result = "0x2a"
                elif method == "eth_getBlockByNumber":
                    block = int(entry["params"][0], 16)
                    result = {"number": hex(block), "timestamp": hex(100 + block * 100)}
                else:
                    result = None
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": entry["id"], "result": result}))

    def run() -> None:
        loop = asyncio.new_event_loop()
        state["loop"] = loop
        asyncio.set_event_loop(loop)

        async def main() -> None:
            shutdown = asyncio.Event()
            state["shutdown"] = shutdown
            server = await serve(handler, "127.0.0.1", port)
            started.set()
            await shutdown.wait()
            server.close()
            await server.wait_closed()

        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    yield port
    loop = state["loop"]
    loop.call_soon_threadsafe(state["shutdown"].set)
    thread.join(timeout=5)


def test_ws_bridge_block_number(ws_server):
    w3 = make_client(f"ws://127.0.0.1:{ws_server}")
    try:
        assert w3.eth.block_number == 42
    finally:
        w3.provider.close()


def test_ws_bridge_batch(ws_server):
    w3 = make_client(f"ws://127.0.0.1:{ws_server}")
    try:
        assert batch_get_block_timestamps(w3, [1, 2]) == {1: 200, 2: 300}
    finally:
        w3.provider.close()
