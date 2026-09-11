from chainledger.config import Settings


def test_token_addresses_normalized():
    s = Settings(tokens="0xAAA, 0xbbb", rpc_url="http://x", db_url="sqlite://")
    assert s.token_addresses == ["0xaaa", "0xbbb"]


def test_defaults():
    s = Settings(_env_file=None, rpc_url="http://x", db_url="sqlite://")
    assert s.confirmations == 5
    assert s.poll_interval_seconds == 3
    assert s.batch_size == 500
