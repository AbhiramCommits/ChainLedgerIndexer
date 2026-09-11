from datetime import UTC, datetime

from sqlalchemy import select

from chainledger.indexer.decoder import TransferDecoder
from chainledger.indexer.writer import upsert_transfers
from chainledger.models import Token, Transfer
from tests.conftest import load_fixture, make_raw_log

MAX_UINT256 = 2**256 - 1
MAX_UINT256_STR = "115792089237316195423570985008687907853269984665640564039457584007913129639935"
MAX_UINT256_DECIMAL = (
    "115792089237316195423570985008687907853269984665640564039457.584007913129639935"
)

TOKEN = "0x" + "c" * 40
FROM = "0x" + "00" * 19 + "01"
TO = "0x" + "00" * 19 + "02"


def test_decodes_real_mainnet_usdc_log():
    raw = load_fixture("mainnet_usdc_transfer_log.json")
    d = TransferDecoder().decode(raw)
    assert d is not None
    assert d.token_address == "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    assert d.from_address == "0x06cff7088619c7178f5e14f0b119458d08d2f5ef"
    assert d.to_address == "0xe0554a476a092703abdb3ef35c80e0d76d32939f"
    assert d.value == 132122565
    assert type(d.value) is int
    assert d.tx_hash == "0xdf82a55359345935ced1818af1a5b00400d5eab78f1dc32d72e4fdfc6232fe9a"
    assert d.block_number == 25956305
    assert d.log_index == 338


def test_skips_erc721_four_topic_log():
    raw = load_fixture("erc721_transfer_log.json")
    decoder = TransferDecoder()
    assert decoder.decode(raw) is None
    assert decoder.skipped_logs == 1


def test_decodes_programmatic_log():
    d = TransferDecoder().decode(make_raw_log(token=TOKEN, value=10**18))
    assert d is not None
    assert (d.from_address, d.to_address, d.value) == (FROM, TO, 10**18)


def test_skips_wrong_topic0():
    raw = make_raw_log()
    raw["topics"] = ["0xdeadbeef"]
    assert TransferDecoder().decode(raw) is None


def test_skips_malformed_log_without_raising():
    raw = make_raw_log()
    del raw["data"]
    assert TransferDecoder().decode(raw) is None


def test_skipped_logs_accumulates():
    decoder = TransferDecoder()
    decoder.decode(load_fixture("erc721_transfer_log.json"))
    decoder.decode(load_fixture("erc721_transfer_log.json"))
    decoder.decode(make_raw_log())
    assert decoder.skipped_logs == 2


async def test_max_uint256_roundtrip_decode_db_api(db_session, api_client):
    token = "0x" + "d" * 40
    db_session.add(Token(address=token, symbol="BIG", name="BIG", decimals=18))
    db_session.commit()

    decoded = TransferDecoder().decode(make_raw_log(token=token, block=7, value=MAX_UINT256))
    assert decoded is not None
    assert type(decoded.value) is int

    inserted, skipped = upsert_transfers(
        db_session,
        [
            {
                "tx_hash": decoded.tx_hash,
                "log_index": decoded.log_index,
                "block_number": decoded.block_number,
                "block_time": datetime.fromtimestamp(1_700_000_000, tz=UTC),
                "token_address": decoded.token_address,
                "from_address": decoded.from_address,
                "to_address": decoded.to_address,
                "value": decoded.value,
            }
        ],
    )
    assert (inserted, skipped) == (1, 0)
    db_session.commit()

    stored = db_session.scalar(select(Transfer.value))
    assert int(stored) == MAX_UINT256

    resp = await api_client.get(f"/transfers?token={token}")
    item = resp.json()["items"][0]
    assert item["value"] == MAX_UINT256_STR
    assert item["value_decimal"] == MAX_UINT256_DECIMAL
