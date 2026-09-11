import math
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import structlog
from sqlalchemy import select

from chainledger.config import Settings
from chainledger.db import SessionLocal
from chainledger.indexer.decoder import TransferDecoder
from chainledger.indexer.rpc import make_client
from chainledger.indexer.service import IndexerService
from chainledger.indexer.writer import (
    build_transfer_rows,
    get_cursor,
    record_coverage,
    upsert_transfers,
)
from chainledger.models import IndexedRange

logger = structlog.get_logger()


@dataclass(frozen=True)
class ChunkResult:
    from_block: int
    to_block: int
    logs_found: int
    rows_inserted: int
    rows_already: int
    skipped_logs: int

    @property
    def blocks(self) -> int:
        return self.to_block - self.from_block + 1


def compute_gaps(
    start_block: int, end_block: int, covered: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Complement of covered ranges within [start_block, end_block].

    Covered ranges are merged (adjacent ranges touch), so the result is the
    minimal set of uncovered block ranges.
    """
    if end_block < start_block:
        return []
    merged: list[tuple[int, int]] = []
    for f, t in sorted(covered):
        if merged and f <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], t))
        else:
            merged.append((f, t))
    gaps: list[tuple[int, int]] = []
    pos = start_block
    for f, t in merged:
        if f > pos:
            gaps.append((pos, min(f - 1, end_block)))
        pos = max(pos, t + 1)
        if pos > end_block:
            break
    if pos <= end_block:
        gaps.append((pos, end_block))
    return gaps


class Backfiller:
    """Re-indexes block ranges using the same fetch_range + upsert paths as
    the live indexer, in parallel across a worker thread pool.

    Writes are ON CONFLICT DO NOTHING, so re-running a covered range inserts
    zero rows. Workers are threads (not processes) because each chunk creates
    its own Web3 client, decoder, and DB session: web3 clients hold
    unpicklable HTTP sessions, and threads are enough for I/O-bound RPC work.
    """

    def __init__(self, settings: Settings, token: str, workers: int = 1) -> None:
        self.settings = settings
        self.token = token
        self.workers = max(1, workers)

    def _chunks(self, from_block: int, to_block: int) -> Iterator[tuple[int, int]]:
        total = to_block - from_block + 1
        if self.workers == 1:
            size = self.settings.batch_size
        else:
            # ~8 chunks per worker keeps executor queue small while staying
            # below provider getLogs limits; fetch_range bisects if needed.
            size = max(self.settings.batch_size, math.ceil(total / (self.workers * 8)))
        for start in range(from_block, to_block + 1, size):
            yield start, min(start + size - 1, to_block)

    def _index_chunk(self, from_block: int, to_block: int) -> ChunkResult:
        w3 = make_client(self.settings.rpc_url)
        decoder = TransferDecoder()
        service = IndexerService(self.settings, w3, decoder=decoder)
        with SessionLocal() as session:
            service.ensure_token(session, self.token)
        raw_logs = service.fetch_range(self.token, from_block, to_block)
        decoded = [d for d in (decoder.decode(log) for log in raw_logs) if d is not None]
        timestamps = service.block_times(d.block_number for d in decoded)
        rows = build_transfer_rows(decoded, timestamps)
        with SessionLocal() as session:
            inserted, skipped = upsert_transfers(session, rows)
            record_coverage(session, self.token, from_block, to_block)
            session.commit()
        return ChunkResult(
            from_block=from_block,
            to_block=to_block,
            logs_found=len(raw_logs),
            rows_inserted=inserted,
            rows_already=skipped,
            skipped_logs=decoder.skipped_logs,
        )

    def run(self, ranges: Sequence[tuple[int, int]]) -> Iterator[ChunkResult]:
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [
                pool.submit(self._index_chunk, f, t)
                for from_block, to_block in ranges
                for f, t in self._chunks(from_block, to_block)
            ]
            for future in as_completed(futures):
                yield future.result()

    def _covered_ranges(self, from_block: int, to_block: int) -> list[tuple[int, int]]:
        with SessionLocal() as session:
            rows = session.execute(
                select(IndexedRange.from_block, IndexedRange.to_block)
                .where(
                    IndexedRange.token_address == self.token,
                    IndexedRange.to_block >= from_block,
                    IndexedRange.from_block <= to_block,
                )
                .order_by(IndexedRange.from_block)
            ).all()
        return [(int(row.from_block), int(row.to_block)) for row in rows]

    def range_fully_covered(self, from_block: int, to_block: int) -> bool:
        return not compute_gaps(from_block, to_block, self._covered_ranges(from_block, to_block))

    def find_gap_ranges(self) -> list[tuple[int, int]]:
        """Uncovered ranges between START_BLOCK and the indexer cursor."""
        with SessionLocal() as session:
            cursor = get_cursor(session, self.token)
        start = self.settings.start_block
        end = cursor if cursor is not None else start - 1
        if end < start:
            return []
        covered = self._covered_ranges(start, end)
        gaps = compute_gaps(start, end, covered)
        logger.info(
            "gap scan",
            token=self.token,
            start_block=start,
            end_block=end,
            covered_ranges=len(covered),
            gaps=len(gaps),
        )
        return gaps
