from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


def format_value_decimal(value: int | Decimal | str, decimals: int) -> str:
    """Format a raw uint256 amount using the token's decimals.

    Computed with Decimal arithmetic (never float) and rendered with exactly
    `decimals` fraction digits, e.g. raw 1e18 with decimals 18 -> "1.000000000000000000".
    """
    scaled = Decimal(value).scaleb(-decimals)
    return f"{scaled:.{decimals}f}"


class HealthToken(BaseModel):
    address: str
    latest_block: int | None = None
    blocks_behind: int | None = None


class HealthResponse(BaseModel):
    status: str
    chain_head: int | None = None
    tokens: list[HealthToken]


class TokenItem(BaseModel):
    address: str
    symbol: str
    name: str
    decimals: int
    transfer_count: int


class TokenListResponse(BaseModel):
    items: list[TokenItem]


class TransferItem(BaseModel):
    tx_hash: str
    log_index: int
    block_number: int
    block_time: datetime
    token_address: str
    symbol: str | None = None
    from_address: str
    to_address: str
    # Raw uint256 as a decimal string: JS cannot represent these as numbers.
    value: str
    # Human-readable amount formatted with the token's decimals.
    value_decimal: str | None = None


class TransferListResponse(BaseModel):
    items: list[TransferItem]
    next_cursor: str | None = None


class TxTransfersResponse(BaseModel):
    items: list[TransferItem]


class BalanceDeltaResponse(BaseModel):
    address: str
    token: str | None = None
    balance_delta: str
    balance_delta_decimal: str | None = None
