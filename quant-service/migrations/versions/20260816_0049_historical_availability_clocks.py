"""Keep historical strategy availability distinct from local ingestion time.

Revision ID: 20260816_0049
Revises: 20260816_0048
"""

from alembic import op


revision = "20260816_0049"
down_revision = "20260816_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable by design: legacy rows predate the dual-clock contract and must
    # remain explicitly distinguishable from newly projected historical data.
    op.execute("ALTER TABLE quant.tushare_raw_records ADD COLUMN IF NOT EXISTS ingested_at timestamptz")
    op.execute("ALTER TABLE quant.tushare_raw_records ADD COLUMN IF NOT EXISTS availability_basis text")
    op.execute("ALTER TABLE quant.raw_market_observations ADD COLUMN IF NOT EXISTS ingested_at timestamptz")
    op.execute("ALTER TABLE quant.raw_market_observations ADD COLUMN IF NOT EXISTS availability_basis text")
    op.execute("""
        CREATE INDEX IF NOT EXISTS tushare_raw_availability_basis_idx
            ON quant.tushare_raw_records(api_name, availability_basis, available_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS raw_market_availability_basis_idx
            ON quant.raw_market_observations(capability, availability_basis, available_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.raw_market_availability_basis_idx")
    op.execute("DROP INDEX IF EXISTS quant.tushare_raw_availability_basis_idx")
    op.execute("ALTER TABLE quant.raw_market_observations DROP COLUMN IF EXISTS availability_basis")
    op.execute("ALTER TABLE quant.raw_market_observations DROP COLUMN IF EXISTS ingested_at")
    op.execute("ALTER TABLE quant.tushare_raw_records DROP COLUMN IF EXISTS availability_basis")
    op.execute("ALTER TABLE quant.tushare_raw_records DROP COLUMN IF EXISTS ingested_at")
