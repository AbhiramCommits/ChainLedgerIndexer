from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from chainledger.indexer.decoder import DecodedTransfer
from chainledger.models import IndexedRange, IndexerCursor, Transfer


def build_transfer_rows(
    decoded: Sequence[DecodedTransfer], timestamps: Mapping[int, int]
) -> list[dict[str, Any]]:
    """Map decoded transfers + block timestamps to insert-ready row dicts."""
    return [
        {
            "tx_hash": d.tx_hash,
            "log_index": d.log_index,
            "block_number": d.block_number,
            "block_time": datetime.fromtimestamp(timestamps[d.block_number], tz=UTC),
            "token_address": d.token_address,
            "from_address": d.from_address,
            "to_address": d.to_address,
            "value": d.value,
        }
        for d in decoded
    ]


def upsert_transfers(session: Session, rows: Sequence[dict[str, Any]]) -> tuple[int, int]:
    """Bulk insert transfers with ON CONFLICT (tx_hash, log_index) DO NOTHING.

    Returns (inserted, skipped). Because duplicates are skipped rather than
    overwritten, re-running an already-indexed block range is a safe no-op.
    The inserted count comes from RETURNING, which only yields rows that were
    actually written (psycopg's rowcount is unreliable for this statement).
    """
    if not rows:
        return 0, 0
    stmt = pg_insert(Transfer).values(list(rows))
    insert_stmt = stmt.on_conflict_do_nothing(index_elements=["tx_hash", "log_index"]).returning(
        Transfer.id
    )
    result = session.execute(insert_stmt)
    inserted = len(result.all())
    return inserted, len(rows) - inserted


def set_cursor(session: Session, token_address: str, last_indexed_block: int) -> None:
    """Upsert the per-token indexer cursor.

    Must be called in the SAME transaction as the matching insert and committed
    together with it, so a crash can never advance the cursor past unwritten
    rows.
    """
    stmt = pg_insert(IndexerCursor).values(
        token_address=token_address,
        last_indexed_block=last_indexed_block,
        updated_at=datetime.now(UTC),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["token_address"],
        set_={
            "last_indexed_block": stmt.excluded.last_indexed_block,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    session.execute(stmt)


def get_cursor(session: Session, token_address: str) -> int | None:
    row = session.get(IndexerCursor, token_address)
    return row.last_indexed_block if row is not None else None


def record_coverage(session: Session, token_address: str, from_block: int, to_block: int) -> None:
    """Record that a block range was successfully indexed.

    Idempotent (ON CONFLICT DO NOTHING); used by the live indexer and by
    backfill --gap-scan to find uncovered ranges below the cursor.
    """
    stmt = pg_insert(IndexedRange).values(
        token_address=token_address,
        from_block=from_block,
        to_block=to_block,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["token_address", "from_block", "to_block"])
    session.execute(stmt)
