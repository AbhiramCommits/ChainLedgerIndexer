"""create indexed_ranges coverage table and transfers pagination index

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "indexed_ranges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("from_block", sa.BigInteger(), nullable=False),
        sa.Column("to_block", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token_address", "from_block", "to_block", name="uq_indexed_ranges_token_range"
        ),
    )
    op.create_index(
        "ix_indexed_ranges_token_from", "indexed_ranges", ["token_address", "from_block"]
    )
    op.create_index(
        "ix_transfers_block_log",
        "transfers",
        [sa.text("block_number DESC"), sa.text("log_index DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_transfers_block_log", table_name="transfers")
    op.drop_index("ix_indexed_ranges_token_from", table_name="indexed_ranges")
    op.drop_table("indexed_ranges")
