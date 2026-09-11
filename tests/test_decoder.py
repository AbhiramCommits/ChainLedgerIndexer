from chainledger.indexer.decoder import TransferDecoder

TOKEN = "0x" + "c" * 40
FROM = "0x" + "00" * 19 + "01"
TO = "0x" + "00" * 19 + "02"
TX_HASH = "0x" + "ab" * 32


def make_raw_log(**kwargs):
    from tests.conftest import make_raw_log as _make

    return _make(**kwargs)


def test_decodes_erc20_transfer():
    decoder = TransferDecoder()
    raw = make_raw_log(token=TOKEN, value=10**18)
    d = decoder.decode(raw)
    assert d is not None
    assert d.tx_hash == TX_HASH
    assert d.log_index == 2
    assert d.block_number == 100
    assert d.token_address == TOKEN
    assert d.from_address == FROM
    assert d.to_address == TO
    assert d.value == 10**18
    assert type(d.value) is int


def test_value_stays_int_for_large_amounts():
    decoder = TransferDecoder()
    raw = make_raw_log(value=10**30)
    d = decoder.decode(raw)
    assert d is not None
    assert d.value == 10**30
    assert type(d.value) is int


def test_skips_erc721_four_topic_transfer():
    decoder = TransferDecoder()
    assert decoder.decode(make_raw_log(n_topics=4)) is None
    assert decoder.skipped_logs == 1


def test_skips_wrong_topic0():
    decoder = TransferDecoder()
    raw = make_raw_log()
    raw["topics"] = ["0xdeadbeef"]
    assert decoder.decode(raw) is None
    assert decoder.skipped_logs == 1


def test_skips_malformed_log_without_raising():
    decoder = TransferDecoder()
    raw = make_raw_log()
    del raw["data"]
    assert decoder.decode(raw) is None
    assert decoder.skipped_logs == 1


def test_skips_missing_topics():
    decoder = TransferDecoder()
    raw = make_raw_log()
    del raw["topics"]
    assert decoder.decode(raw) is None


def test_skipped_logs_accumulates():
    decoder = TransferDecoder()
    decoder.decode(make_raw_log(n_topics=4))
    decoder.decode(make_raw_log(n_topics=4))
    decoder.decode(make_raw_log())
    assert decoder.skipped_logs == 2


def test_decodes_erc20_shape():
    decoder = TransferDecoder()
    d = decoder.decode(make_raw_log(n_topics=3))
    assert d is not None
