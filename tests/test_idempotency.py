from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from chainledger.config import Settings
from chainledger.indexer.service import IndexerService
from chainledger.indexer.writer import get_cursor, set_cursor, upsert_transfers
from chainledger.models import IndexerCursor, Token, Transfer
from tests.conftest import BLOCK_TIMESTAMP, FakeWeb3, make_raw_log

TOKEN = "0x" + "a" * 40


def make_settings(**kwargs) -> Settings:
    return Settings(_env_file=None, rpc_url="http://fake", db_url="postgresql://fake", **kwargs)


def make_w3(logs: list[dict], latest: int = 100) -> FakeWeb3:
    return FakeWeb3(
        latest_block=latest,
        logs=logs,
        timestamps={log["blockNumber"]: BLOCK_TIMESTAMP for log in logs},
    )


def transfer_row(tx: int, block: int = 100, value: int = 10**18) -> dict:
    return {
        "tx_hash": "0x" + f"{tx:064x}",
        "log_index": tx,
        "block_number": block,
        "block_time": datetime.fromtimestamp(BLOCK_TIMESTAMP, tz=UTC),
        "token_address": TOKEN,
        "from_address": "0x" + "00" * 19 + "01",
        "to_address": "0x" + "00" * 19 + "02",
        "value": value,
    }


def ensure_token(db_session) -> None:
    db_session.add(Token(address=TOKEN, symbol="T", name="T", decimals=18))
    db_session.commit()


def test_upsert_inserts_and_skips_duplicates(db_session):
    ensure_token(db_session)
    rows = [transfer_row(1), transfer_row(2)]
    assert upsert_transfers(db_session, rows) == (2, 0)
    db_session.commit()
    # Re-running the same range is a safe no-op thanks to ON CONFLICT.
    assert upsert_transfers(db_session, rows + [transfer_row(3)]) == (1, 2)
    db_session.commit()
    # Duplicates within a single statement are also skipped.
    assert upsert_transfers(db_session, [transfer_row(4), transfer_row(4)]) == (1, 1)
    db_session.commit()


def test_transfer_requires_token_fk(db_session):
    with pytest.raises(IntegrityError):
        upsert_transfers(db_session, [transfer_row(7)])
        db_session.commit()
    db_session.rollback()


def test_cursor_roundtrip(db_session):
    assert get_cursor(db_session, TOKEN) is None
    set_cursor(db_session, TOKEN, 100)
    db_session.commit()
    assert get_cursor(db_session, TOKEN) == 100
    set_cursor(db_session, TOKEN, 250)
    db_session.commit()
    assert get_cursor(db_session, TOKEN) == 250


def test_index_same_range_twice_is_idempotent(db_session, session_factory):
    logs = [
        make_raw_log(token=TOKEN, block=10),
        make_raw_log(token=TOKEN, block=11, tx_hash=b"\xcd" * 32, log_index=3),
    ]
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=0),
        make_w3(logs, latest=11),
        session_factory=session_factory,
    )
    service.run_once()
    assert db_session.scalar(select(func.count(Transfer.id))) == 2
    service.run_once()
    assert db_session.scalar(select(func.count(Transfer.id))) == 2


def test_second_pass_reports_zero_inserted(db_session, session_factory, monkeypatch):
    logs = [make_raw_log(token=TOKEN, block=10)]
    results: list[tuple[int, int]] = []
    real_upsert = upsert_transfers

    def spy(session, rows):
        result = real_upsert(session, rows)
        results.append(result)
        return result

    monkeypatch.setattr("chainledger.indexer.service.upsert_transfers", spy)
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=0),
        make_w3(logs, latest=10),
        session_factory=session_factory,
    )
    service.run_once()
    # Simulate a resync: rewind the cursor so the same range is indexed again.
    db_session.execute(delete(IndexerCursor).where(IndexerCursor.token_address == TOKEN))
    db_session.commit()
    service.run_once()
    assert results[0] == (1, 0)
    assert results[1] == (0, 1)


def test_crash_before_commit_writes_nothing(db_session, session_factory):
    ensure_token(db_session)
    session = session_factory()
    try:
        upsert_transfers(session, [transfer_row(1), transfer_row(2)])
        set_cursor(session, TOKEN, 99)
        raise RuntimeError("simulated crash before commit")
    except RuntimeError:
        session.rollback()
    finally:
        session.close()

    with session_factory() as check:
        assert check.scalar(select(func.count(Transfer.id))) == 0
        assert get_cursor(check, TOKEN) is None
