import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from chainledger.config import Settings
from chainledger.db import Base
from chainledger.indexer.service import IndexerService
from chainledger.indexer.writer import get_cursor, set_cursor, upsert_transfers
from chainledger.models import Token, Transfer
from tests.conftest import make_raw_log

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(TEST_DB_URL is None, reason="TEST_DB_URL not set")


@pytest.fixture
def session_factory():
    engine = create_engine(TEST_DB_URL)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    Base.metadata.drop_all(engine)


def transfer_row(tx: int, block: int = 100, value: int = 10**18) -> dict:
    return {
        "tx_hash": "0x" + f"{tx:064x}",
        "log_index": tx,
        "block_number": block,
        "block_time": datetime.fromtimestamp(1_700_000_000, tz=UTC),
        "token_address": "0x" + "a" * 40,
        "from_address": "0x" + "00" * 19 + "01",
        "to_address": "0x" + "00" * 19 + "02",
        "value": value,
    }


def test_upsert_inserts_and_skips_duplicates(session_factory):
    token = "0x" + "a" * 40
    with session_factory() as session:
        session.add(Token(address=token, symbol="T", name="T", decimals=18))
        session.commit()
        rows = [transfer_row(1), transfer_row(2)]
        assert upsert_transfers(session, rows) == (2, 0)
        session.commit()
        # Re-running the same range is a safe no-op thanks to ON CONFLICT.
        assert upsert_transfers(session, rows + [transfer_row(3)]) == (1, 2)
        session.commit()
        # Duplicates within a single statement are also skipped.
        assert upsert_transfers(session, [transfer_row(4), transfer_row(4)]) == (1, 1)
        session.commit()


def test_upsert_value_stored_exactly(session_factory):
    token = "0x" + "a" * 40
    with session_factory() as session:
        session.add(Token(address=token, symbol="T", name="T", decimals=18))
        session.commit()
        upsert_transfers(session, [transfer_row(9, value=10**30)])
        session.commit()
        assert session.scalar(select(Transfer.value)) == 10**30


def test_transfer_requires_token_fk(session_factory):
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            upsert_transfers(session, [transfer_row(7)])
            session.commit()
        session.rollback()


def test_cursor_roundtrip(session_factory):
    token = "0x" + "b" * 40
    with session_factory() as session:
        assert get_cursor(session, token) is None
        set_cursor(session, token, 100)
        session.commit()
        assert get_cursor(session, token) == 100
        set_cursor(session, token, 250)
        session.commit()
        assert get_cursor(session, token) == 250


# --- full pipeline with fake RPC --------------------------------------------


class FakeCall:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class FakeContract:
    functions = SimpleNamespace(
        symbol=lambda: FakeCall("TST"),
        decimals=lambda: FakeCall(18),
    )


class FakeEth:
    def __init__(self, logs, latest: int, timestamps: dict[int, int]):
        self.logs = logs
        self.latest = latest
        self.timestamps = timestamps

    def get_logs(self, params):
        return [
            log
            for log in self.logs
            if params["fromBlock"] <= log["blockNumber"] <= params["toBlock"]
        ]

    @property
    def block_number(self):
        return self.latest

    def contract(self, address, abi):
        return FakeContract()


class FakeProvider:
    def __init__(self, timestamps: dict[int, int]):
        self.timestamps = timestamps

    def make_batch_request(self, requests):
        return [
            {"id": i, "result": {"number": hex(n), "timestamp": hex(self.timestamps[n])}}
            for i, (_, params) in enumerate(requests)
            for n in [int(params[0], 16)]
        ]


class FakeW3:
    def __init__(self, eth):
        self.eth = eth
        self.provider = FakeProvider(eth.timestamps)


TOKEN = "0x" + "c" * 40
BLOCK_TIME = 1_700_000_000


def test_run_once_end_to_end(session_factory):
    raw = make_raw_log(token=TOKEN, block=10, value=10**18)
    w3 = FakeW3(FakeEth(logs=[raw], latest=12, timestamps={10: BLOCK_TIME}))
    settings = Settings(
        rpc_url="http://fake",
        db_url="postgresql://fake",
        tokens=TOKEN,
        confirmations=2,
        start_block=0,
    )
    service = IndexerService(settings, w3, session_factory=session_factory)

    service.run_once()

    with session_factory() as session:
        transfer = session.scalar(select(Transfer))
        assert transfer is not None
        assert transfer.value == 10**18
        assert transfer.block_number == 10
        assert transfer.block_time == datetime.fromtimestamp(BLOCK_TIME, tz=UTC)
        assert transfer.from_address == "0x" + "00" * 19 + "01"
        assert get_cursor(session, TOKEN) == 10
        token_row = session.get(Token, TOKEN)
        assert token_row is not None
        assert (token_row.symbol, token_row.decimals) == ("TST", 18)

    # Second pass over the same range must be a no-op.
    service.run_once()
    with session_factory() as session:
        assert session.scalar(select(func.count(Transfer.id))) == 1
        assert get_cursor(session, TOKEN) == 10


def test_run_once_respects_confirmations(session_factory):
    # Latest block 13 with 2 confirmations -> safe head 11, so the log in
    # block 12 must not be indexed yet.
    raw = make_raw_log(token=TOKEN, block=12, value=5)
    w3 = FakeW3(FakeEth(logs=[raw], latest=13, timestamps={12: BLOCK_TIME}))
    settings = Settings(
        rpc_url="http://fake",
        db_url="postgresql://fake",
        tokens=TOKEN,
        confirmations=2,
        start_block=0,
    )
    service = IndexerService(settings, w3, session_factory=session_factory)
    service.run_once()
    with session_factory() as session:
        assert session.scalar(select(func.count(Transfer.id))) == 0
        assert get_cursor(session, TOKEN) == 11
