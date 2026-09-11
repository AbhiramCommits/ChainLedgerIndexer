from functools import lru_cache

from web3 import Web3

from chainledger.config import get_settings
from chainledger.indexer.rpc import make_client


@lru_cache(maxsize=1)
def _cached_client(rpc_url: str) -> Web3:
    return make_client(rpc_url)


def get_w3() -> Web3:
    return _cached_client(get_settings().rpc_url)
