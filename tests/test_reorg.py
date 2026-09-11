from sqlalchemy import func, select

from chainledger.indexer.service import IndexerService
from chainledger.indexer.writer import get_cursor
from chainledger.models import Transfer
from tests.conftest import BLOCK_TIMESTAMP, FakeWeb3, make_raw_log
from tests.test_idempotency import make_settings

TOKEN = "0x" + "a" * 40


def test_never_indexes_past_safe_head(db_session, session_factory):
    # latest 100 with 5 confirmations -> safe head 95; logs in blocks 96/97
    # must not be touched even though getLogs would return them.
    logs = [
        make_raw_log(token=TOKEN, block=95),
        make_raw_log(token=TOKEN, block=96, tx_hash=b"\xcd" * 32),
        make_raw_log(token=TOKEN, block=97, tx_hash=b"\xce" * 32),
    ]
    w3 = FakeWeb3(
        latest_block=100,
        logs=logs,
        timestamps={log["blockNumber"]: BLOCK_TIMESTAMP for log in logs},
    )
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=5),
        w3,
        session_factory=session_factory,
    )
    service.run_once()

    with session_factory() as session:
        blocks = list(session.scalars(select(Transfer.block_number)))
        assert blocks == [95]
        assert get_cursor(session, TOKEN) == 95


def test_safe_head_moves_forward_with_chain(db_session, session_factory):
    logs = [make_raw_log(token=TOKEN, block=95)]
    w3 = FakeWeb3(
        latest_block=100,
        logs=logs,
        timestamps={log["blockNumber"]: BLOCK_TIMESTAMP for log in logs},
    )
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=5),
        w3,
        session_factory=session_factory,
    )
    service.run_once()
    with session_factory() as session:
        assert get_cursor(session, TOKEN) == 95
        assert session.scalar(select(func.count(Transfer.id))) == 1

    # Chain advances to 110: safe head 105, but no new logs in 96..105.
    w3.latest_block = 110
    service.run_once()
    with session_factory() as session:
        assert get_cursor(session, TOKEN) == 105
        assert session.scalar(select(func.count(Transfer.id))) == 1


def test_confirmations_zero_indexes_through_head(db_session, session_factory):
    logs = [make_raw_log(token=TOKEN, block=100)]
    w3 = FakeWeb3(
        latest_block=100,
        logs=logs,
        timestamps={log["blockNumber"]: BLOCK_TIMESTAMP for log in logs},
    )
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=0),
        w3,
        session_factory=session_factory,
    )
    service.run_once()
    with session_factory() as session:
        assert session.scalar(select(func.count(Transfer.id))) == 1
        assert get_cursor(session, TOKEN) == 100
