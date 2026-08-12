"""Add display name to the bounded board-stock mining evidence.

Revision ID: 20260812_0010
Revises: 20260812_0009
"""

from alembic import op


revision = "20260812_0010"
down_revision = "20260812_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.intraday_board_stock_mining_candidates ADD COLUMN IF NOT EXISTS name text")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.intraday_board_stock_mining_candidates DROP COLUMN IF EXISTS name")
