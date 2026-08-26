"""Per-stock end-of-day capital flow.

Sector flow was ingested; per-stock flow never was.  The only per-stock main
flow anywhere in this system was a research-only value scraped live from a
public Eastmoney endpoint and discarded when the scan ended, so no post-close
study could ask whether main flow preceded anything.

Revision ID: 20260826_0067
Revises: 20260826_0066
"""

from alembic import op


revision = "20260826_0067"
down_revision = "20260826_0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``source`` is part of the key on purpose: moneyflow, moneyflow_dc and
    # moneyflow_ths each define their order-size buckets differently, so their
    # rows are stored side by side rather than merged into one number that no
    # vendor actually published.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.stock_money_flow_daily (
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            trading_date date NOT NULL,
            source text NOT NULL,
            provider text NOT NULL DEFAULT 'tushare',
            net_amount numeric,
            net_amount_rate numeric,
            buy_elg_amount numeric,
            buy_lg_amount numeric,
            buy_md_amount numeric,
            buy_sm_amount numeric,
            available_at timestamptz NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol, trading_date, source)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS stock_money_flow_daily_date_idx
            ON quant.stock_money_flow_daily(trading_date, source)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.stock_money_flow_daily")
