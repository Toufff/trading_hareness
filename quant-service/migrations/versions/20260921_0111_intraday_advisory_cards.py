"""Native Feishu card payloads for advisory deliveries.

Revision ID: 20260921_0111
Revises: 20260921_0110
"""

from alembic import op


revision = "20260921_0111"
down_revision = "20260921_0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.intraday_advisory_deliveries
        ADD COLUMN IF NOT EXISTS message_card jsonb NOT NULL DEFAULT '{}'::jsonb
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE quant.intraday_advisory_deliveries DROP COLUMN IF EXISTS message_card")
