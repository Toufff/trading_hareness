"""Add source-unit-safe daily whole-market aggregates.

Revision ID: 20260816_0035
Revises: 20260815_0034
"""

from alembic import op


revision = "20260816_0035"
down_revision = "20260815_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.daily_market_aggregates (
            trading_date date PRIMARY KEY,
            stock_count integer NOT NULL CHECK (stock_count >= 0),
            advancers integer NOT NULL CHECK (advancers >= 0),
            decliners integer NOT NULL CHECK (decliners >= 0),
            unchanged integer NOT NULL CHECK (unchanged >= 0),
            median_change_pct numeric,
            mean_change_pct numeric,
            total_amount_kcny numeric,
            total_volume_lots numeric,
            source_provider text NOT NULL,
            available_at timestamptz NOT NULL,
            quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS daily_market_aggregates_date_idx
            ON quant.daily_market_aggregates(trading_date DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.daily_market_aggregates_date_idx")
    op.execute("DROP TABLE IF EXISTS quant.daily_market_aggregates")
