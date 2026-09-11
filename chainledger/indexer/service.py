import signal
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from web3 import Web3

from chainledger.config import Settings
from chainledger.db import SessionLocal
from chainledger.indexer.decoder import (
    TRANSFER_TOPIC0,
    TransferDecoder,
    load_erc20_abi,
)
from chainledger.indexer.rpc import batch_get_block_timestamps
from chainledger.indexer.writer import (
    build_transfer_rows,
    get_cursor,
    record_coverage,
    set_cursor,
    upsert_transfers,
)
from chainledger.models import Token

logger = structlog.get_logger()

SessionFactory = Callable[[], Session]

_RANGE_ERROR_MARKERS = (
    "more than",
    "too many",
    "range too large",
    "block range",
    "result limit",
    "limited to",
)

_BLOCK_TIME_CACHE_LIMIT = 100_000


class IndexerService:
    def __init__(
        self,
        settings: Settings,
        w3: Web3,
        decoder: TransferDecoder | None = None,
        session_factory: SessionFactory = SessionLocal,
    ) -> None:
        self.settings = settings
        self.w3 = w3
        self.decoder = decoder or TransferDecoder()
        self.session_factory = session_factory
        self._block_time_cache: dict[int, int] = {}
        self._abi = load_erc20_abi()

    # -- RPC ----------------------------------------------------------------

    def fetch_range(self, token: str, from_block: int, to_block: int) -> list[dict[str, Any]]:
        try:
            logs = self.w3.eth.get_logs(
                {
                    "address": Web3.to_checksum_address(token),
                    "topics": [TRANSFER_TOPIC0],
                    "fromBlock": from_block,
                    "toBlock": to_block,
                }
            )
        except Exception as exc:
            if from_block >= to_block or not self._is_range_error(exc):
                raise
            mid = (from_block + to_block) // 2
            logger.warning(
                "getLogs range too large, bisecting",
                token=token,
                from_block=from_block,
                to_block=to_block,
                mid=mid,
            )
            return [
                *self.fetch_range(token, from_block, mid),
                *self.fetch_range(token, mid + 1, to_block),
            ]
        return [dict(log) for log in logs]

    @staticmethod
    def _is_range_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in _RANGE_ERROR_MARKERS)

    def block_times(self, block_numbers: Iterable[int]) -> dict[int, int]:
        """Timestamp lookup with a per-block cache; misses are fetched with a
        single batch RPC call, so many logs in the same block share one request."""
        unique = set(block_numbers)
        missing = [n for n in unique if n not in self._block_time_cache]
        if missing:
            self._block_time_cache.update(batch_get_block_timestamps(self.w3, missing))
        if len(self._block_time_cache) > _BLOCK_TIME_CACHE_LIMIT:
            cutoff = max(self._block_time_cache) - _BLOCK_TIME_CACHE_LIMIT // 2
            self._block_time_cache = {
                n: ts for n, ts in self._block_time_cache.items() if n >= cutoff
            }
        return {n: self._block_time_cache[n] for n in unique}

    # -- token metadata -------------------------------------------------------

    def ensure_token(self, session: Session, token: str) -> None:
        existing = session.get(Token, token)
        # Unknown metadata (e.g. the token wasn't deployed yet when first
        # seen) is retried on subsequent cycles.
        if existing is not None and existing.symbol != "UNKNOWN":
            return
        symbol, decimals = self._fetch_token_metadata(token)
        if existing is None:
            # ON CONFLICT DO NOTHING: concurrent workers (backfill) may race
            # to register the same token; only one row may exist.
            stmt = pg_insert(Token).values(
                address=token, symbol=symbol, name=symbol, decimals=decimals
            )
            session.execute(stmt.on_conflict_do_nothing(index_elements=["address"]))
            session.commit()
            logger.info("token registered", token=token, symbol=symbol, decimals=decimals)
        else:
            existing.symbol = symbol
            existing.name = symbol
            existing.decimals = decimals
            session.commit()
            logger.info("token metadata updated", token=token, symbol=symbol, decimals=decimals)

    def _fetch_token_metadata(self, token: str) -> tuple[str, int]:
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=self._abi)
        try:
            symbol = str(contract.functions.symbol().call())
        except Exception:
            symbol = "UNKNOWN"
        if not symbol:
            symbol = "UNKNOWN"
        try:
            decimals = int(contract.functions.decimals().call())
        except Exception:
            decimals = 18
        return symbol, decimals

    # -- indexing -------------------------------------------------------------

    def _index_token(self, token: str, stop_event: threading.Event | None = None) -> None:
        with self.session_factory() as session:
            self.ensure_token(session, token)

        with self.session_factory() as session:
            cursor = get_cursor(session, token)
        current = cursor if cursor is not None else self.settings.start_block - 1

        latest = self.w3.eth.block_number
        safe_head = latest - self.settings.confirmations
        if safe_head <= current:
            return

        while current < safe_head:
            if stop_event is not None and stop_event.is_set():
                return
            to_block = min(current + self.settings.batch_size, safe_head)
            started = time.monotonic()

            raw_logs = self.fetch_range(token, current + 1, to_block)
            decoded = [d for d in (self.decoder.decode(log) for log in raw_logs) if d is not None]
            timestamps = self.block_times(d.block_number for d in decoded)
            rows = build_transfer_rows(decoded, timestamps)

            # Cursor advance and coverage record share the transaction with
            # the inserts: all are committed (or rolled back) together, so the
            # cursor can never pass unwritten data even if the process crashes
            # mid-batch.
            with self.session_factory() as session:
                inserted, skipped = upsert_transfers(session, rows)
                set_cursor(session, token, to_block)
                record_coverage(session, token, current + 1, to_block)
                session.commit()

            duration_ms = (time.monotonic() - started) * 1000
            logger.info(
                "batch indexed",
                token=token,
                from_block=current + 1,
                to_block=to_block,
                logs_found=len(raw_logs),
                rows_inserted=inserted,
                rows_skipped=skipped,
                skipped_logs=self.decoder.skipped_logs,
                duration_ms=round(duration_ms, 2),
            )
            current = to_block

    def run_once(self, stop_event: threading.Event | None = None) -> None:
        tokens = self.settings.token_addresses
        if not tokens:
            logger.warning("no tokens configured, nothing to index")
            return
        for token in tokens:
            self._index_token(token, stop_event)

    def run_forever(self) -> None:
        logger.info(
            "indexer starting",
            rpc_url=self.settings.rpc_url,
            chain_id=self.settings.chain_id,
            tokens=self.settings.token_addresses,
            start_block=self.settings.start_block,
            confirmations=self.settings.confirmations,
        )
        stop_event = threading.Event()

        def _handle_signal(signum: int, frame: Any) -> None:
            logger.info("shutdown signal received, finishing current batch", signal=signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        while not stop_event.is_set():
            try:
                self.run_once(stop_event)
            except Exception:
                logger.exception("indexing cycle failed")
            stop_event.wait(self.settings.poll_interval_seconds)
        logger.info("indexer stopped")
