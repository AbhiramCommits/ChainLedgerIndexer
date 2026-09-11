import os
import signal
import threading

from sqlalchemy import func, select

from chainledger.config import Settings
from chainledger.indexer.service import IndexerService
from chainledger.models import Token, Transfer
from tests.conftest import BLOCK_TIMESTAMP, FakeWeb3, make_raw_log

TOKEN = "0x" + "a" * 40


def make_settings(**kwargs) -> Settings:
    return Settings(_env_file=None, rpc_url="http://fake", db_url="postgresql://fake", **kwargs)


def make_w3(logs: list[dict], latest: int = 100, **kwargs) -> FakeWeb3:
    return FakeWeb3(
        latest_block=latest,
        logs=logs,
        timestamps={log["blockNumber"]: BLOCK_TIMESTAMP for log in logs},
        **kwargs,
    )


def test_ensure_token_metadata_fallback(db_session, session_factory):
    w3 = make_w3([], latest=5, symbol_fails=True, decimals_fail=True)
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=5),
        w3,
        session_factory=session_factory,
    )
    service.run_once()
    row = db_session.get(Token, TOKEN)
    assert row is not None
    assert row.symbol == "UNKNOWN"
    assert row.decimals == 18


def test_ensure_token_refreshes_unknown_metadata(db_session, session_factory):
    broken = make_w3([], latest=5, symbol_fails=True, decimals_fail=True)
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=5),
        broken,
        session_factory=session_factory,
    )
    service.run_once()
    assert db_session.get(Token, TOKEN).symbol == "UNKNOWN"

    healthy = make_w3([], latest=5, symbol="TST", decimals=6)
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=5),
        healthy,
        session_factory=session_factory,
    )
    service.run_once()
    row = db_session.get(Token, TOKEN)
    assert (row.symbol, row.decimals) == ("TST", 6)


def test_batch_indexes_multiple_batches_per_cycle(db_session, session_factory):
    logs = [
        make_raw_log(token=TOKEN, block=b, tx_hash=b.to_bytes(32, "big"), log_index=0)
        for b in (5, 20, 45)
    ]
    w3 = make_w3(logs, latest=50)
    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=0, batch_size=10),
        w3,
        session_factory=session_factory,
    )
    service.run_once()
    assert db_session.scalar(select(func.count(Transfer.id))) == 3


def test_run_once_honours_stop_event(db_session, session_factory):
    import threading

    service = IndexerService(
        make_settings(tokens=TOKEN, confirmations=0),
        make_w3([], latest=5),
        session_factory=session_factory,
    )
    stop = threading.Event()
    stop.set()
    service.run_once(stop)
    assert db_session.scalar(select(func.count(Transfer.id))) == 0


def test_block_time_cache_prunes_oldest(monkeypatch):
    monkeypatch.setattr("chainledger.indexer.service._BLOCK_TIME_CACHE_LIMIT", 2)
    w3 = FakeWeb3(latest_block=10, timestamps={1: 100, 2: 200, 3: 300})
    service = IndexerService(make_settings(tokens=TOKEN), w3)
    assert service.block_times([1, 2, 3]) == {1: 100, 2: 200, 3: 300}
    assert 1 not in service._block_time_cache
    assert 3 in service._block_time_cache


def test_run_forever_recovers_from_cycle_error(db_session, session_factory, monkeypatch):
    service = IndexerService(
        make_settings(tokens="", poll_interval_seconds=0.1),
        make_w3([], latest=5),
        session_factory=session_factory,
    )
    cycles: list[int] = []

    def flaky_run_once(stop_event=None):
        cycles.append(1)
        if len(cycles) == 1:
            raise RuntimeError("boom")
        return None

    monkeypatch.setattr(service, "run_once", flaky_run_once)
    original_handler = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.6, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        service.run_forever()
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, original_handler)
    assert len(cycles) >= 2


def test_run_forever_stops_on_sigint(db_session, session_factory, monkeypatch):
    service = IndexerService(
        make_settings(tokens="", poll_interval_seconds=0.1),
        make_w3([], latest=5),
        session_factory=session_factory,
    )
    cycles: list[int] = []

    def fake_run_once(stop_event=None):
        cycles.append(1)
        return None

    monkeypatch.setattr(service, "run_once", fake_run_once)
    original_handler = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.4, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        service.run_forever()
    finally:
        timer.cancel()
        signal.signal(signal.SIGINT, original_handler)
    assert len(cycles) >= 1
