import pytest
from web3.exceptions import Web3RPCError

from chainledger.config import Settings
from chainledger.indexer.service import IndexerService

TOKEN = "0x" + "a" * 40


def make_service(w3, **kwargs) -> IndexerService:
    settings = Settings(
        rpc_url="http://fake",
        db_url="postgresql://fake",
        tokens=TOKEN,
        **kwargs,
    )
    return IndexerService(settings, w3)


class BisectEth:
    def __init__(self, max_width: int):
        self.max_width = max_width
        self.calls: list[tuple[int, int]] = []

    def get_logs(self, params):
        from_block, to_block = params["fromBlock"], params["toBlock"]
        self.calls.append((from_block, to_block))
        if to_block - from_block + 1 > self.max_width:
            raise Web3RPCError("query returned more than 10000 results")
        return []


class FakeWeb3:
    def __init__(self, eth):
        self.eth = eth


def test_fetch_range_bisects_on_range_error():
    eth = BisectEth(max_width=2)
    service = make_service(FakeWeb3(eth))
    assert service.fetch_range(TOKEN, 0, 7) == []
    assert eth.calls == [(0, 7), (0, 3), (0, 1), (2, 3), (4, 7), (4, 5), (6, 7)]


def test_fetch_range_no_bisection_for_small_ranges():
    eth = BisectEth(max_width=100)
    service = make_service(FakeWeb3(eth))
    assert service.fetch_range(TOKEN, 0, 7) == []
    assert eth.calls == [(0, 7)]


def test_fetch_range_single_block_error_raises():
    eth = BisectEth(max_width=0)
    service = make_service(FakeWeb3(eth))
    with pytest.raises(Web3RPCError):
        service.fetch_range(TOKEN, 5, 5)


class BoomEth:
    def get_logs(self, params):
        raise ValueError("boom")


def test_fetch_range_propagates_non_range_errors():
    service = make_service(FakeWeb3(BoomEth()))
    with pytest.raises(ValueError):
        service.fetch_range(TOKEN, 0, 10)


class BlockTimeProvider:
    def __init__(self, timestamps: dict[int, int]):
        self.timestamps = timestamps
        self.calls = 0

    def make_batch_request(self, requests):
        self.calls += 1
        return [
            {"id": i, "result": {"number": hex(n), "timestamp": hex(self.timestamps[n])}}
            for i, (_, params) in enumerate(requests)
            for n in [int(params[0], 16)]
        ]


def test_block_times_cached_per_block():
    provider = BlockTimeProvider({10: 1000, 11: 2000})
    w3 = FakeWeb3(None)
    w3.provider = provider
    service = make_service(w3)
    assert service.block_times([10, 10, 11]) == {10: 1000, 11: 2000}
    assert provider.calls == 1
    assert service.block_times([10]) == {10: 1000}
    assert provider.calls == 1
