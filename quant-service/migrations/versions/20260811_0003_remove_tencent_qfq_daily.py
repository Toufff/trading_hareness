"""Remove legacy Tencent front-adjusted daily rows from derived bar tables.

Raw observations remain intact for source attribution and short-window study
display.  Tencent's public adapter uses qfq prices, while canonical daily bars
are unadjusted with a separately captured adjustment factor.

Revision ID: 20260811_0003
Revises: 20260811_0002
"""

from alembic import op


revision = "20260811_0003"
down_revision = "20260811_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A canonical row selected from Tencent has the wrong price basis.  Rows
    # selected from another provider can retain Tencent raw observation IDs as
    # non-selected evidence and are therefore not touched.
    op.execute("DELETE FROM quant.canonical_bars_daily WHERE selected_provider = 'tencent_free'")
    op.execute("DELETE FROM quant.market_bars_daily WHERE source = 'tencent_free'")


def downgrade() -> None:
    # The removed data is intentionally recoverable only from its raw evidence;
    # recreating it here would reintroduce the mixed-price-basis defect.
    pass
