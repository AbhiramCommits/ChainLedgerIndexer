import pytest
from fastapi.testclient import TestClient

from chainledger.api import app
from chainledger.db import get_db


class FakeSession:
    def execute(self, *args, **kwargs):
        return None


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: FakeSession()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
