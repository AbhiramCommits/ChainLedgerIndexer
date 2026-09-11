from web3.types import HexBytes

from chainledger.indexer.decoder import TRANSFER_TOPIC0


def make_raw_log(
    *,
    token: str = "0x" + "c" * 40,
    block: int = 100,
    log_index: int = 2,
    tx_index: int = 7,
    value: int = 10**18,
    n_topics: int = 3,
    tx_hash: bytes = b"\xab" * 32,
) -> dict:
    topics = [HexBytes(TRANSFER_TOPIC0)]
    if n_topics >= 2:
        topics.append(HexBytes((1).to_bytes(32, "big")))
    if n_topics >= 3:
        topics.append(HexBytes((2).to_bytes(32, "big")))
    if n_topics >= 4:
        topics.append(HexBytes((1234).to_bytes(32, "big")))
    return {
        "topics": topics,
        "data": HexBytes(value.to_bytes(32, "big")),
        "transactionHash": HexBytes(tx_hash),
        "blockHash": HexBytes(b"\xcd" * 32),
        "logIndex": log_index,
        "blockNumber": block,
        "transactionIndex": tx_index,
        "address": token,
    }
