import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from chainledger.api import app
from chainledger.api.deps import get_w3
from chainledger.db import Base, get_db
from chainledger.models import IndexerCursor, Token, Transfer

TEST_DB_URL = os.environ.get("TEST_DB_URL")

pytestmark = pytest.mark.skipif(TEST_DB_URL is None, reason="TEST_DB_URL not set")

A = "0x" + "a" * 40
B = "0x" + "b" * 40
ADDR1 = "0x" + "11" * 20
ADDR2 = "0x" + "22" * 20
ADDR3 = "0x" + "33" * 20
TX1 = "0x" + "01" * 32
TX2 = "0x" + "02" * 32
TX3 = "0x" + "03" * 32
TX4 = "0x" + "04" * 32

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def seed(session) -> None:
    session.add_all(
        [
            Token(address=A, symbol="TST", name="Test Token", decimals=18),
            Token(address=B, symbol="USDC", name="USD Coin", decimals=6),
        ]
    )
    session.add_all(
        [
            Transfer(
                tx_hash=TX1,
                log_index=0,
                block_number=1,
                block_time=T0,
                token_address=A,
                from_address=ADDR1,
                to_address=ADDR2,
                value=10**18,
            ),
            Transfer(
                tx_hash=TX2,
                log_index=0,
                block_number=2,
                block_time=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
                token_address=A,
                from_address=ADDR2,
                to_address=ADDR3,
                value=2 * 10**18,
            ),
            Transfer(
                tx_hash=TX3,
                log_index=1,
                block_number=2,
                block_time=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
                token_address=A,
                from_address=ADDR3,
                to_address=ADDR1,
                value=5 * 10**17,
            ),
            Transfer(
                tx_hash=TX4,
                log_index=0,
                block_number=4,
                block_time=datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
                token_address=B,
                from_address=ADDR1,
                to_address=ADDR3,
                value=1234567,
            ),
        ]
    )
    session.add_all(
        [
            IndexerCursor(token_address=A, last_indexed_block=5, updated_at=T0),
            IndexerCursor(token_address=B, last_indexed_block=5, updated_at=T0),
        ]
    )
    session.commit()


class FakeEth:
    @property
    def block_number(self):
        return 100


class FakeW3:
    def __init__(self):
        self.eth = FakeEth()


@pytest.fixture
def client():
    engine = create_engine(TEST_DB_URL)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    def override_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    with TestSession() as session:
        seed(session)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_w3] = lambda: FakeW3()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["chain_head"] == 100
    tokens = {t["address"]: t for t in body["tokens"]}
    assert tokens[A]["latest_block"] == 5
    assert tokens[A]["blocks_behind"] == 95


def test_tokens(client):
    body = client.get("/tokens").json()
    items = {t["address"]: t for t in body["items"]}
    assert items[A]["transfer_count"] == 3
    assert items[B]["transfer_count"] == 1
    assert items[A]["decimals"] == 18


def test_transfers_pagination_walk(client):
    seen = []
    url = "/transfers?limit=2"
    while url:
        body = client.get(url).json()
        assert len(body["items"]) <= 2
        seen.extend((i["block_number"], i["log_index"]) for i in body["items"])
        url = f"/transfers?limit=2&cursor={body['next_cursor']}" if body["next_cursor"] else None
    assert seen == [(4, 0), (2, 1), (2, 0), (1, 0)]
    assert len(seen) == len(set(seen))


def test_transfers_order_desc(client):
    body = client.get("/transfers").json()
    keys = [(i["block_number"], i["log_index"]) for i in body["items"]]
    assert keys == sorted(keys, reverse=True)


def test_transfers_filters(client):
    assert len(client.get(f"/transfers?token={A}").json()["items"]) == 3
    assert len(client.get("/transfers?from_block=1&to_block=2").json()["items"]) == 3
    assert len(client.get(f"/transfers?address={ADDR1}").json()["items"]) == 3
    items = client.get(f"/transfers?from_address={ADDR2}&to_address={ADDR3}").json()["items"]
    assert [i["tx_hash"] for i in items] == [TX2]
    items = client.get(
        "/transfers?start_time=2026-01-01T00:30:00Z&end_time=2026-01-01T02:30:00Z"
    ).json()["items"]
    assert len(items) == 3


def test_transfers_value_serialization(client):
    items = client.get(f"/transfers?token={A}&to_block=1").json()["items"]
    assert items[0]["value"] == "1000000000000000000"
    assert items[0]["value_decimal"] == "1.000000000000000000"
    items = client.get(f"/transfers?token={B}").json()["items"]
    assert items[0]["value"] == "1234567"
    assert items[0]["value_decimal"] == "1.234567"


def test_transfers_by_tx(client):
    items = client.get(f"/transfers/{TX2}").json()["items"]
    assert [i["tx_hash"] for i in items] == [TX2]
    assert items[0]["from_address"] == ADDR2


def test_balance_delta(client):
    body = client.get(f"/addresses/{ADDR3}/balance-delta?token={A}").json()
    assert body["balance_delta"] == "1500000000000000000"
    assert body["balance_delta_decimal"] == "1.500000000000000000"
    body = client.get(f"/addresses/{ADDR1}/balance-delta?token={A}").json()
    assert body["balance_delta"] == "-500000000000000000"
    assert body["balance_delta_decimal"] == "-0.500000000000000000"
    body = client.get(f"/addresses/{ADDR1}/balance-delta?token={B}").json()
    assert body["balance_delta"] == "-1234567"
    assert body["balance_delta_decimal"] == "-1.234567"
    body = client.get(f"/addresses/{ADDR1}/balance-delta").json()
    assert body["balance_delta"] == "-500000000001234567"
    assert body["balance_delta_decimal"] is None


def test_balance_delta_unknown_token_404(client):
    unknown = "0x" + "dd" * 20
    resp = client.get(f"/addresses/{ADDR1}/balance-delta?token={unknown}")
    assert resp.status_code == 404


def test_validation_422(client):
    assert client.get("/transfers?from_address=0x123").status_code == 422
    assert client.get("/transfers?from_block=5&to_block=1").status_code == 422
    assert client.get("/transfers?cursor=%21%21%21").status_code == 422
    assert client.get("/transfers?limit=501").status_code == 422
    assert client.get("/addresses/0x123/balance-delta").status_code == 422
    assert client.get("/transfers/0xzz").status_code == 422
