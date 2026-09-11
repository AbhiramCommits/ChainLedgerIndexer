from datetime import datetime
from typing import Annotated

import structlog
from eth_utils.hexadecimal import is_hexstr
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select, text, tuple_
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from web3 import Web3

from chainledger.api.deps import get_w3
from chainledger.api.pagination import decode_cursor, encode_cursor
from chainledger.api.schemas import (
    BalanceDeltaResponse,
    HealthResponse,
    HealthToken,
    TokenItem,
    TokenListResponse,
    TransferItem,
    TransferListResponse,
    TxTransfersResponse,
    format_value_decimal,
)
from chainledger.db import get_db
from chainledger.models import IndexerCursor, Token, Transfer

logger = structlog.get_logger()

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]
RpcClient = Annotated[Web3, Depends(get_w3)]


def parse_address(value: str) -> str:
    """Validate a hex address and normalize it to lowercase."""
    if not Web3.is_address(value):
        raise HTTPException(status_code=422, detail=f"invalid address: {value}")
    return str(Web3.to_checksum_address(value)).lower()


def parse_tx_hash(value: str) -> str:
    if not is_hexstr(value) or len(value) != 66:
        raise HTTPException(status_code=422, detail=f"invalid tx hash: {value}")
    return value.lower()


def _time_range_valid(start: datetime | None, end: datetime | None) -> bool:
    if start is None or end is None:
        return True
    if (start.tzinfo is None) != (end.tzinfo is None):
        return False
    return start <= end


def _transfer_item(row: Row[tuple[Transfer, str, int]]) -> TransferItem:
    transfer, symbol, decimals = row
    return TransferItem(
        tx_hash=transfer.tx_hash,
        log_index=transfer.log_index,
        block_number=transfer.block_number,
        block_time=transfer.block_time,
        token_address=transfer.token_address,
        symbol=symbol,
        from_address=transfer.from_address,
        to_address=transfer.to_address,
        value=str(transfer.value),
        value_decimal=format_value_decimal(transfer.value, decimals),
    )


@router.get("/health", response_model=HealthResponse)
def health(db: DbSession, w3: RpcClient) -> HealthResponse:
    try:
        db.execute(text("SELECT 1"))
    except OperationalError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    try:
        chain_head = w3.eth.block_number
    except Exception:
        logger.warning("rpc unavailable for health check")
        chain_head = None
    rows = db.execute(
        select(IndexerCursor.token_address, IndexerCursor.last_indexed_block).order_by(
            IndexerCursor.token_address
        )
    ).all()
    tokens = [
        HealthToken(
            address=row.token_address,
            latest_block=row.last_indexed_block,
            blocks_behind=(
                max(0, chain_head - row.last_indexed_block) if chain_head is not None else None
            ),
        )
        for row in rows
    ]
    return HealthResponse(
        status="ok" if chain_head is not None else "degraded",
        chain_head=chain_head,
        tokens=tokens,
    )


@router.get("/tokens", response_model=TokenListResponse)
def list_tokens(db: DbSession) -> TokenListResponse:
    transfer_count = (
        select(Transfer.token_address, func.count(Transfer.id).label("count"))
        .group_by(Transfer.token_address)
        .subquery()
    )
    rows = db.execute(
        select(Token, func.coalesce(transfer_count.c.count, 0))
        .outerjoin(transfer_count, Token.address == transfer_count.c.token_address)
        .order_by(Token.address)
    ).all()
    return TokenListResponse(
        items=[
            TokenItem(
                address=token.address,
                symbol=token.symbol,
                name=token.name,
                decimals=token.decimals,
                transfer_count=int(count),
            )
            for token, count in rows
        ]
    )


@router.get("/transfers", response_model=TransferListResponse)
def list_transfers(
    db: DbSession,
    token: str | None = None,
    from_address: str | None = None,
    to_address: str | None = None,
    address: str | None = None,
    from_block: Annotated[int | None, Query(ge=0)] = None,
    to_block: Annotated[int | None, Query(ge=0)] = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: str | None = None,
) -> TransferListResponse:
    token_addr = parse_address(token) if token else None
    from_addr = parse_address(from_address) if from_address else None
    to_addr = parse_address(to_address) if to_address else None
    any_addr = parse_address(address) if address else None

    if from_block is not None and to_block is not None and from_block > to_block:
        raise HTTPException(status_code=422, detail="from_block must be <= to_block")
    if not _time_range_valid(start_time, end_time):
        raise HTTPException(
            status_code=422, detail="start_time must be <= end_time (and same tz awareness)"
        )

    conditions = []
    if token_addr is not None:
        conditions.append(Transfer.token_address == token_addr)
    if from_addr is not None:
        conditions.append(Transfer.from_address == from_addr)
    if to_addr is not None:
        conditions.append(Transfer.to_address == to_addr)
    if any_addr is not None:
        conditions.append(or_(Transfer.from_address == any_addr, Transfer.to_address == any_addr))
    if from_block is not None:
        conditions.append(Transfer.block_number >= from_block)
    if to_block is not None:
        conditions.append(Transfer.block_number <= to_block)
    if start_time is not None:
        conditions.append(Transfer.block_time >= start_time)
    if end_time is not None:
        conditions.append(Transfer.block_time <= end_time)

    # Keyset pagination, NOT OFFSET: OFFSET cost grows linearly with page
    # number (the DB scans and discards N*limit rows), while a cursor over
    # (block_number, log_index) rides the DESC index and stays constant per
    # page regardless of table size.
    stmt = (
        select(Transfer, Token.symbol, Token.decimals)
        .join(Token, Transfer.token_address == Token.address)
        .where(*conditions)
    )
    if cursor is not None:
        cursor_block, cursor_log_index = decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(Transfer.block_number, Transfer.log_index) < (cursor_block, cursor_log_index)
        )
    stmt = stmt.order_by(Transfer.block_number.desc(), Transfer.log_index.desc()).limit(limit + 1)

    rows = list(db.execute(stmt).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1][0]
        next_cursor = encode_cursor(last.block_number, last.log_index)
    return TransferListResponse(
        items=[_transfer_item(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/transfers/{tx_hash}", response_model=TxTransfersResponse)
def transfers_by_tx(db: DbSession, tx_hash: str) -> TxTransfersResponse:
    tx = parse_tx_hash(tx_hash)
    rows = db.execute(
        select(Transfer, Token.symbol, Token.decimals)
        .join(Token, Transfer.token_address == Token.address)
        .where(Transfer.tx_hash == tx)
        .order_by(Transfer.log_index.asc())
    ).all()
    return TxTransfersResponse(items=[_transfer_item(row) for row in rows])


@router.get("/addresses/{address}/balance-delta", response_model=BalanceDeltaResponse)
def balance_delta(db: DbSession, address: str, token: str | None = None) -> BalanceDeltaResponse:
    addr = parse_address(address)
    token_addr = parse_address(token) if token else None
    decimals: int | None = None
    if token_addr is not None:
        token_row = db.get(Token, token_addr)
        if token_row is None:
            raise HTTPException(status_code=404, detail="token not indexed")
        decimals = token_row.decimals

    inflow = func.coalesce(
        func.sum(case((Transfer.to_address == addr, Transfer.value), else_=0)), 0
    )
    outflow = func.coalesce(
        func.sum(case((Transfer.from_address == addr, Transfer.value), else_=0)), 0
    )
    stmt = select(inflow - outflow).where(
        or_(Transfer.from_address == addr, Transfer.to_address == addr)
    )
    if token_addr is not None:
        stmt = stmt.where(Transfer.token_address == token_addr)
    delta = db.scalar(stmt)
    raw = str(int(delta)) if delta is not None else "0"
    return BalanceDeltaResponse(
        address=addr,
        token=token_addr,
        balance_delta=raw,
        balance_delta_decimal=format_value_decimal(raw, decimals) if decimals is not None else None,
    )
