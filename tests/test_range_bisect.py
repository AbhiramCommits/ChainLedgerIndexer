import pytest
from web3.exceptions import Web3RPCError

from chainledger.indexer.service import IndexerService
from tests.conftest import BLOCK_TIMESTAMP, FakeWeb3, make_raw_log
from tests.test_idempotency import make_settings

TOKEN = "0x" + "a" * 40


def test_fetch_range_bisects_on_range_error():
    logs = [
        make_raw_log(token=TOKEN, block=i, tx_hash=i.to_bytes(32, "big"), log_index=0)
        for i in range(0, 100)
    ]
    w3 = FakeWeb3(
        latest_block=100,
        logs=logs,
        timestamps={i: BLOCK_TIMESTAMP for i in range(0, 100)},
        max_logs_width=10,
    )
    service = IndexerService(make_settings(tokens=TOKEN), w3)

    result = service.fetch_range(TOKEN, 0, 99)

    blocks = [log["blockNumber"] for log in result]
    assert sorted(blocks) == list(range(100))
    assert len(set(blocks)) == 100  # every log exactly once
    # The fake must never have been asked for a range wider than its limit.
    assert all(to - f + 1 <= 10 for f, to in w3.calls)


def test_fetch_range_bisects_deeply():
    logs = [
        make_raw_log(token=TOKEN, block=i, tx_hash=i.to_bytes(32, "big"), log_index=0)
        for i in range(0, 100)
    ]
    w3 = FakeWeb3(
        latest_block=100,
        logs=logs,
        timestamps={i: BLOCK_TIMESTAMP for i in range(0, 100)},
        max_logs_width=1,
    )
    service = IndexerService(make_settings(tokens=TOKEN), w3)
    result = service.fetch_range(TOKEN, 0, 99)
    assert len(result) == 100
    assert all(to == f for f, to in w3.calls)  # bisected down to single blocks


def test_single_block_range_error_raises():
    w3 = FakeWeb3(latest_block=5, max_logs_width=0)
    service = IndexerService(make_settings(tokens=TOKEN), w3)
    with pytest.raises(Web3RPCError):
        service.fetch_range(TOKEN, 5, 5)


def test_non_range_error_propagates():
    class BoomWeb3(FakeWeb3):
        pass

    w3 = BoomWeb3(latest_block=5)

    def boom(params):
        raise ValueError("provider exploded")

    w3.eth.get_logs = boom  # type: ignore[method-assign]
    service = IndexerService(make_settings(tokens=TOKEN), w3)
    with pytest.raises(ValueError):
        service.fetch_range(TOKEN, 0, 10)


def test_block_times_fetched_once_per_block():
    w3 = FakeWeb3(latest_block=100, timestamps={10: 1000, 11: 2000})
    service = IndexerService(make_settings(tokens=TOKEN), w3)
    assert service.block_times([10, 10, 11]) == {10: 1000, 11: 2000}
    # Second call is served entirely from the cache: no new batch RPC.
    assert service.block_times([10]) == {10: 1000}
