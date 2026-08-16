"""Preserve a vendor availability clock for offline minute replay.

Revision ID: 20260816_0047
Revises: 20260816_0046
"""

from alembic import op


revision = "20260816_0047"
down_revision = "20260816_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.market_bars_minute ADD COLUMN IF NOT EXISTS source_available_at timestamptz")
    op.execute("""
        CREATE INDEX IF NOT EXISTS market_bars_minute_source_availability_idx
            ON quant.market_bars_minute(source_available_at, bar_time)
            WHERE source_available_at IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.market_bars_minute_source_availability_idx")
    op.execute("ALTER TABLE quant.market_bars_minute DROP COLUMN IF EXISTS source_available_at")
