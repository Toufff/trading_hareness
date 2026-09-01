"""Store adjusted research bars separately from raw execution prices.

Revision ID: 20260901_0082
Revises: 20260901_0081
"""

from alembic import op


revision = "20260901_0082"
down_revision = "20260901_0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.research_adjusted_bars_daily (
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            trading_date date NOT NULL,
            open numeric,
            high numeric,
            low numeric,
            close numeric NOT NULL CHECK (close > 0),
            volume numeric,
            pct_change numeric,
            adjustment_basis text NOT NULL CHECK (adjustment_basis IN ('qfq','hfq')),
            provider text NOT NULL,
            source_artifact_sha256 text NOT NULL CHECK (source_artifact_sha256 ~ '^[0-9a-f]{64}$'),
            source_available_at timestamptz,
            imported_at timestamptz NOT NULL DEFAULT now(),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol,trading_date,adjustment_basis,provider)
        );
        CREATE INDEX IF NOT EXISTS research_adjusted_bars_date_idx
            ON quant.research_adjusted_bars_daily(trading_date DESC,symbol);
        CREATE INDEX IF NOT EXISTS research_adjusted_bars_symbol_idx
            ON quant.research_adjusted_bars_daily(symbol,trading_date DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.research_adjusted_bars_daily")
