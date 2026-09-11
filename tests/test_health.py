import pytest
from fastapi.testclient import TestClient

from chainledger.api import app
from chainledger.api.deps import get_w3
from chainledger.db import get_db


class FakeResult:
    def all(self):
        return []

    def scalar(self):
        return None


class FakeSession:
    def execute(self, *args, **kwargs):
        return FakeResult()

    def get(self, *args, **kwargs):
        return None


class FakeEth:
    @property
    def block_number(self):
        return 100


class FakeWeb3:
    def __init__(self):
        self.eth = FakeEth()


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: FakeSession()
    app.dependency_overrides[get_w3] = lambda: FakeWeb3()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["chain_head"] == 100
    assert body["tokens"] == []
