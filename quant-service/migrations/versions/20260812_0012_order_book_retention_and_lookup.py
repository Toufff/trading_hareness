"""Index bounded Tencent order-book reads.

Revision ID: 20260812_0012
Revises: 20260812_0011
"""

from alembic import op


revision = "20260812_0012"
down_revision = "20260812_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_order_book_recent_idx
        ON quant.intraday_quote_observations(symbol, observed_at DESC)
        WHERE source_name='tencent_order_book'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.intraday_order_book_recent_idx")
