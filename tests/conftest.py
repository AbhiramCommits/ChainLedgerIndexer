import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Bind the module-level engine in chainledger/db.py to a harmless URL before
# any chainledger import. Tests never use it: they inject sessions bound to
# the testcontainers database via fixtures below.
os.environ.setdefault("DB_URL", "postgresql+psycopg://unused:unused@localhost:5432/unused")

import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402
from web3.exceptions import Web3RPCError  # noqa: E402
from web3.types import HexBytes  # noqa: E402

from alembic import command  # noqa: E402
from chainledger.api import app  # noqa: E402
from chainledger.api.deps import get_w3  # noqa: E402
from chainledger.config import get_settings  # noqa: E402
from chainledger.db import get_db  # noqa: E402
from chainledger.indexer.decoder import TRANSFER_TOPIC0  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TABLES = ("transfers", "indexed_ranges", "indexer_cursors", "tokens")

BLOCK_TIMESTAMP = 1_700_000_000


def load_fixture(name: str) -> dict | list:
    return json.loads((FIXTURES_DIR / name).read_text())


# --- database ----------------------------------------------------------------


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    with PostgresContainer("postgres:16") as postgres:
        url = postgres.get_connection_url().replace("+psycopg2", "+psycopg")
        os.environ["DB_URL"] = url
        get_settings.cache_clear()
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        command.upgrade(cfg, "head")
        yield url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Iterator[Engine]:
    engine = create_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def clean_db(db_engine: Engine) -> Iterator[None]:
    with db_engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    yield


@pytest.fixture
def db_session(session_factory: sessionmaker[Session], clean_db: None) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


# --- fake web3 ---------------------------------------------------------------


class FakeCall:
    def __init__(self, value, fail: bool = False) -> None:
        self._value = value
        self._fail = fail

    def call(self):
        if self._fail:
            raise Web3RPCError("execution reverted")
        return self._value


class FakeContract:
    def __init__(self, fake: "FakeWeb3") -> None:
        self.functions = SimpleNamespace(
            symbol=lambda: FakeCall(fake.symbol, fail=fake.symbol_fails),
            decimals=lambda: FakeCall(fake.decimals, fail=fake.decimals_fail),
        )


class FakeEth:
    def __init__(self, fake: "FakeWeb3") -> None:
        self._fake = fake

    @property
    def block_number(self) -> int:
        return self._fake.latest_block

    def get_logs(self, params: dict) -> list[dict]:
        from_block, to_block = params["fromBlock"], params["toBlock"]
        if self._fake.max_logs_width is not None and to_block - from_block + 1 > (
            self._fake.max_logs_width
        ):
            raise Web3RPCError("query returned more than 10000 results")
        # Only successful fetches are recorded, so callers can assert every
        # served range respected the provider limit.
        self._fake.calls.append((from_block, to_block))
        return [log for log in self._fake.logs if from_block <= log["blockNumber"] <= to_block]

    def contract(self, address, abi) -> FakeContract:
        return FakeContract(self._fake)


class FakeProvider:
    def __init__(self, fake: "FakeWeb3") -> None:
        self._fake = fake

    def make_batch_request(self, requests):
        return [
            {
                "id": i,
                "result": {
                    "number": hex(block),
                    "timestamp": hex(self._fake.timestamps[block]),
                },
            }
            for i, (_, params) in enumerate(requests)
            for block in [int(params[0], 16)]
        ]


class FakeWeb3:
    """Web3 double serving canned eth_getLogs / eth_blockNumber / eth_getBlock
    payloads (optionally loaded from tests/fixtures/*.json via `logs_file`)."""

    def __init__(
        self,
        *,
        latest_block: int,
        logs: list[dict] | None = None,
        logs_file: str | None = None,
        timestamps: dict[int, int] | None = None,
        max_logs_width: int | None = None,
        symbol: str = "TST",
        decimals: int = 18,
        symbol_fails: bool = False,
        decimals_fail: bool = False,
    ) -> None:
        self.latest_block = latest_block
        if logs is not None:
            self.logs = logs
        elif logs_file is not None:
            self.logs = list(load_fixture(logs_file))
        else:
            self.logs = []
        self.timestamps = timestamps or {}
        self.max_logs_width = max_logs_width
        self.symbol = symbol
        self.decimals = decimals
        self.symbol_fails = symbol_fails
        self.decimals_fail = decimals_fail
        self.calls: list[tuple[int, int]] = []
        self.eth = FakeEth(self)
        self.provider = FakeProvider(self)


# --- api client --------------------------------------------------------------


@pytest.fixture
def fake_w3() -> FakeWeb3:
    return FakeWeb3(latest_block=100)


@pytest.fixture
async def api_client(
    db_engine: Engine, clean_db: None, fake_w3: FakeWeb3
) -> AsyncIterator[AsyncClient]:
    TestSession = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_w3] = lambda: fake_w3
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# --- helpers -----------------------------------------------------------------


def make_raw_log(
    *,
    token: str = "0x" + "c" * 40,
    block: int = 100,
    log_index: int = 2,
    tx_index: int = 7,
    value: int = 10**18,
    n_topics: int = 3,
    tx_hash: bytes = b"\xab" * 32,
) -> dict:
    topics = [HexBytes(TRANSFER_TOPIC0)]
    if n_topics >= 2:
        topics.append(HexBytes((1).to_bytes(32, "big")))
    if n_topics >= 3:
        topics.append(HexBytes((2).to_bytes(32, "big")))
    if n_topics >= 4:
        topics.append(HexBytes((1234).to_bytes(32, "big")))
    return {
        "topics": topics,
        "data": HexBytes(value.to_bytes(32, "big")),
        "transactionHash": HexBytes(tx_hash),
        "blockHash": HexBytes(b"\xcd" * 32),
        "logIndex": log_index,
        "blockNumber": block,
        "transactionIndex": tx_index,
        "address": token,
    }
