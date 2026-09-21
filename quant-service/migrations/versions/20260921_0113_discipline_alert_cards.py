"""Native Feishu cards for discipline deliveries.

Revision ID: 20260921_0113
Revises: 20260921_0112
"""

from alembic import op


revision = "20260921_0113"
down_revision = "20260921_0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.discipline_alert_deliveries
        ADD COLUMN IF NOT EXISTS message_card jsonb NOT NULL DEFAULT '{}'::jsonb
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE quant.discipline_alert_deliveries DROP COLUMN IF EXISTS message_card")
