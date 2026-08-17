"""Index local historical reprojection by provider, API and trade date.

Revision ID: 20260816_0050
Revises: 20260816_0049
"""

from alembic import op


revision = "20260816_0050"
down_revision = "20260816_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This ledger can be receiving intraday writes while a historical repair
    # starts.  Do not take a table-wide write lock merely to add an expression
    # lookup index.  Fresh databases have no material cost, and an existing
    # manually created index is an idempotent no-op.
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS tushare_raw_provider_api_trade_date_idx
                ON quant.tushare_raw_records(provider_key, api_name, ((row_data->>'trade_date')), record_index)
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.tushare_raw_provider_api_trade_date_idx")
