import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import structlog
from hexbytes import HexBytes
from web3 import Web3
from web3.types import LogReceipt

logger = structlog.get_logger()

ERC20_ABI_PATH = Path(__file__).resolve().parent.parent / "abi" / "erc20.json"

TRANSFER_SIGNATURE = "Transfer(address,address,uint256)"
TRANSFER_TOPIC0 = Web3.to_hex(Web3.keccak(text=TRANSFER_SIGNATURE))


def load_erc20_abi() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], json.loads(ERC20_ABI_PATH.read_text()))


@dataclass(frozen=True)
class DecodedTransfer:
    tx_hash: str
    log_index: int
    block_number: int
    token_address: str
    from_address: str
    to_address: str
    value: int


class TransferDecoder:
    """Decodes raw ERC-20 Transfer logs into DecodedTransfer rows.

    Undecodable logs (wrong topic, malformed payloads, or the ERC-721 variant
    of the Transfer signature) are skipped and counted in ``skipped_logs``.
    Values are kept as Python ints end to end, never floats.
    """

    def __init__(self, abi: list[dict[str, Any]] | None = None) -> None:
        self._event = Web3().eth.contract(abi=abi or load_erc20_abi()).events.Transfer()
        self.skipped_logs = 0

    def decode(self, raw_log: dict[str, Any]) -> DecodedTransfer | None:
        try:
            return self._decode(raw_log)
        except Exception as exc:
            logger.warning(
                "skipping undecodable transfer log",
                tx_hash=str(raw_log.get("transactionHash")),
                log_index=raw_log.get("logIndex"),
                error=str(exc),
            )
            self._skip()
            return None

    def _decode(self, raw_log: dict[str, Any]) -> DecodedTransfer | None:
        topics = [t for t in (raw_log.get("topics") or []) if t is not None]
        # Topics arrive as bytes (HexBytes) from web3 or as hex strings from
        # raw JSON fixtures; normalize both before comparing.
        if not topics or Web3.to_hex(HexBytes(topics[0])).lower() != TRANSFER_TOPIC0.lower():
            self._skip()
            return None
        # ERC-20 and ERC-721 both define `Transfer(address,address,uint256)`,
        # so their topic0 (keccak of the signature) is identical. The log
        # shapes differ: ERC-721 additionally indexes `tokenId`, producing 4
        # topics, while ERC-20 Transfer has exactly 3 topics (from, to, and a
        # non-indexed value). Anything with more than 3 topics is an NFT
        # transfer (or some other event) and must not be indexed as ERC-20.
        if len(topics) > 3:
            self._skip()
            return None
        decoded = self._event.process_log(cast(LogReceipt, raw_log))
        args = decoded["args"]
        return DecodedTransfer(
            tx_hash=Web3.to_hex(HexBytes(raw_log["transactionHash"])).lower(),
            log_index=int(raw_log["logIndex"]),
            block_number=int(raw_log["blockNumber"]),
            token_address=str(raw_log["address"]).lower(),
            from_address=str(args["from"]).lower(),
            to_address=str(args["to"]).lower(),
            value=int(args["value"]),
        )

    def _skip(self) -> None:
        self.skipped_logs += 1
        return None
