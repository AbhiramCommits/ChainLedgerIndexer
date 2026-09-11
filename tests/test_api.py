from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from chainledger.api import app, deps
from chainledger.api.deps import get_w3
from chainledger.db import get_db
from chainledger.models import IndexerCursor, Token, Transfer

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


def seed(db_session) -> None:
    db_session.add_all(
        [
            Token(address=A, symbol="TST", name="Test Token", decimals=18),
            Token(address=B, symbol="USDC", name="USD Coin", decimals=6),
        ]
    )
    db_session.add_all(
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
    db_session.add_all(
        [
            IndexerCursor(token_address=A, last_indexed_block=5, updated_at=T0),
            IndexerCursor(token_address=B, last_indexed_block=5, updated_at=T0),
        ]
    )
    db_session.commit()


async def test_health(db_session, api_client):
    seed(db_session)
    body = (await api_client.get("/health")).json()
    assert body["status"] == "ok"
    assert body["chain_head"] == 100
    tokens = {t["address"]: t for t in body["tokens"]}
    assert tokens[A]["latest_block"] == 5
    assert tokens[A]["blocks_behind"] == 95


async def test_health_degraded_when_rpc_down(db_engine, clean_db):
    class BrokenEth:
        @property
        def block_number(self):
            raise RuntimeError("rpc down")

    broken = SimpleNamespace(eth=BrokenEth())
    TestSession = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override_get_db():
        session = TestSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_w3] = lambda: broken
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            body = (await client.get("/health")).json()
            assert body["status"] == "degraded"
            assert body["chain_head"] is None
    finally:
        app.dependency_overrides.clear()


async def test_health_503_when_db_down(db_engine, clean_db):
    class DownSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception("db down"))

    app.dependency_overrides[get_db] = lambda: DownSession()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/health")).status_code == 503
    finally:
        app.dependency_overrides.clear()


async def test_tokens(db_session, api_client):
    seed(db_session)
    items = {t["address"]: t for t in (await api_client.get("/tokens")).json()["items"]}
    assert items[A]["transfer_count"] == 3
    assert items[B]["transfer_count"] == 1
    assert items[A]["decimals"] == 18


async def test_pagination_returns_each_row_exactly_once(db_session, api_client):
    seed(db_session)
    seen: list[tuple[int, int]] = []
    url = "/transfers?limit=2"
    while url:
        body = (await api_client.get(url)).json()
        assert len(body["items"]) <= 2
        seen.extend((i["block_number"], i["log_index"]) for i in body["items"])
        url = f"/transfers?limit=2&cursor={body['next_cursor']}" if body["next_cursor"] else None
    assert seen == [(4, 0), (2, 1), (2, 0), (1, 0)]
    assert len(seen) == len(set(seen))


async def test_transfers_filters(db_session, api_client):
    seed(db_session)
    assert len((await api_client.get(f"/transfers?token={A}")).json()["items"]) == 3
    assert len((await api_client.get("/transfers?from_block=1&to_block=2")).json()["items"]) == 3
    assert len((await api_client.get(f"/transfers?address={ADDR1}")).json()["items"]) == 3
    items = (await api_client.get(f"/transfers?from_address={ADDR2}&to_address={ADDR3}")).json()[
        "items"
    ]
    assert [i["tx_hash"] for i in items] == [TX2]
    items = (
        await api_client.get(
            "/transfers?start_time=2026-01-01T00:30:00Z&end_time=2026-01-01T02:30:00Z"
        )
    ).json()["items"]
    assert len(items) == 3


async def test_value_serialized_as_decimal_strings(db_session, api_client):
    seed(db_session)
    items = (await api_client.get(f"/transfers?token={A}&to_block=1")).json()["items"]
    assert items[0]["value"] == "1000000000000000000"
    assert items[0]["value_decimal"] == "1.000000000000000000"
    items = (await api_client.get(f"/transfers?token={B}")).json()["items"]
    assert items[0]["value"] == "1234567"
    assert items[0]["value_decimal"] == "1.234567"


async def test_transfers_by_tx(db_session, api_client):
    seed(db_session)
    items = (await api_client.get(f"/transfers/{TX2}")).json()["items"]
    assert [i["tx_hash"] for i in items] == [TX2]


async def test_balance_delta(db_session, api_client):
    seed(db_session)
    body = (await api_client.get(f"/addresses/{ADDR3}/balance-delta?token={A}")).json()
    assert body["balance_delta"] == "1500000000000000000"
    assert body["balance_delta_decimal"] == "1.500000000000000000"
    body = (await api_client.get(f"/addresses/{ADDR1}/balance-delta?token={A}")).json()
    assert body["balance_delta"] == "-500000000000000000"
    body = (await api_client.get(f"/addresses/{ADDR1}/balance-delta")).json()
    assert body["balance_delta"] == "-500000000001234567"
    assert body["balance_delta_decimal"] is None
    unknown = "0x" + "dd" * 20
    resp = await api_client.get(f"/addresses/{ADDR1}/balance-delta?token={unknown}")
    assert resp.status_code == 404


async def test_invalid_inputs_return_422(db_session, api_client):
    seed(db_session)
    assert (await api_client.get("/transfers?from_address=0x123")).status_code == 422
    assert (await api_client.get("/transfers?from_block=5&to_block=1")).status_code == 422
    assert (await api_client.get("/transfers?cursor=%21%21%21")).status_code == 422
    assert (await api_client.get("/transfers?limit=501")).status_code == 422
    assert (await api_client.get("/addresses/0x123/balance-delta")).status_code == 422
    assert (await api_client.get("/transfers/0xzz")).status_code == 422
    assert (
        await api_client.get(
            "/transfers?start_time=2026-01-02T00:00:00Z&end_time=2026-01-01T00:00:00Z"
        )
    ).status_code == 422


def test_get_w3_is_cached(monkeypatch):
    deps._cached_client.cache_clear()
    calls: list[str] = []

    def fake_make_client(rpc_url: str):
        calls.append(rpc_url)
        return object()

    monkeypatch.setattr(deps, "make_client", fake_make_client)
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(rpc_url="http://x"))
    first = deps.get_w3()
    second = deps.get_w3()
    assert first is second
    assert calls == ["http://x"]
    deps._cached_client.cache_clear()


@pytest.mark.parametrize("limit", [1, 50, 500])
async def test_limit_bounds_accepted(db_session, api_client, limit):
    seed(db_session)
    resp = await api_client.get(f"/transfers?limit={limit}")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) <= limit
