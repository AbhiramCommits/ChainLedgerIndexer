"""create tokens, transfers, indexer_cursors

Revision ID: 0001
Revises:
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tokens",
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("address"),
    )

    op.create_table(
        "transfers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tx_hash", sa.Text(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("block_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.ForeignKeyConstraint(["token_address"], ["tokens.address"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tx_hash", "log_index", name="uq_transfers_tx_hash_log_index"),
    )
    op.create_index(
        "ix_transfers_from_block",
        "transfers",
        ["from_address", sa.text("block_number DESC")],
    )
    op.create_index(
        "ix_transfers_to_block",
        "transfers",
        ["to_address", sa.text("block_number DESC")],
    )
    op.create_index(
        "ix_transfers_token_block",
        "transfers",
        ["token_address", sa.text("block_number DESC")],
    )
    op.create_index("ix_transfers_block_number", "transfers", ["block_number"])

    op.create_table(
        "indexer_cursors",
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("last_indexed_block", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("token_address"),
    )


def downgrade() -> None:
    op.drop_table("indexer_cursors")
    op.drop_index("ix_transfers_block_number", table_name="transfers")
    op.drop_index("ix_transfers_token_block", table_name="transfers")
    op.drop_index("ix_transfers_to_block", table_name="transfers")
    op.drop_index("ix_transfers_from_block", table_name="transfers")
    op.drop_table("transfers")
    op.drop_table("tokens")
