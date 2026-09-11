from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint, desc
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TIMESTAMP

from chainledger.db import Base


class Token(Base):
    __tablename__ = "tokens"

    address: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text)
    decimals: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)

    transfers: Mapped[list["Transfer"]] = relationship(back_populates="token")


class Transfer(Base):
    __tablename__ = "transfers"
    __table_args__ = (
        UniqueConstraint("tx_hash", "log_index", name="uq_transfers_tx_hash_log_index"),
        Index("ix_transfers_from_block", "from_address", desc("block_number")),
        Index("ix_transfers_to_block", "to_address", desc("block_number")),
        Index("ix_transfers_token_block", "token_address", desc("block_number")),
        Index("ix_transfers_block_number", "block_number"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tx_hash: Mapped[str] = mapped_column(Text)
    log_index: Mapped[int] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger)
    block_time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    token_address: Mapped[str] = mapped_column(
        Text, ForeignKey("tokens.address", ondelete="CASCADE")
    )
    from_address: Mapped[str] = mapped_column(Text)
    to_address: Mapped[str] = mapped_column(Text)
    value: Mapped[int] = mapped_column(Numeric(78, 0))

    token: Mapped[Token] = relationship(back_populates="transfers")


class IndexerCursor(Base):
    __tablename__ = "indexer_cursors"

    token_address: Mapped[str] = mapped_column(Text, primary_key=True)
    last_indexed_block: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
