"""Make intraday alert delivery durable across a transient adapter failure.

Revision ID: 20260811_0004
Revises: 20260811_0003
"""

from alembic import op


revision = "20260811_0004"
down_revision = "20260811_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.intraday_alert_deliveries ADD COLUMN IF NOT EXISTS message_text text")
    op.execute("ALTER TABLE quant.intraday_alert_deliveries ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE quant.intraday_alert_deliveries ADD COLUMN IF NOT EXISTS next_attempt_at timestamptz")
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_alert_delivery_retry_idx
        ON quant.intraday_alert_deliveries(status, next_attempt_at, created_at)
        WHERE status IN ('pending','failed')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.intraday_alert_delivery_retry_idx")
    op.execute("ALTER TABLE quant.intraday_alert_deliveries DROP COLUMN IF EXISTS next_attempt_at")
    op.execute("ALTER TABLE quant.intraday_alert_deliveries DROP COLUMN IF EXISTS attempt_count")
    op.execute("ALTER TABLE quant.intraday_alert_deliveries DROP COLUMN IF EXISTS message_text")
