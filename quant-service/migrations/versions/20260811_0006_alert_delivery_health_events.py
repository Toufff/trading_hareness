"""Persist Feishu alert failure streaks and one recovery receipt.

Revision ID: 20260811_0006
Revises: 20260811_0005
"""

from alembic import op


revision = "20260811_0006"
down_revision = "20260811_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.alert_delivery_health_events (
            health_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            channel text NOT NULL,
            event_type text NOT NULL CHECK (event_type IN ('failure_streak','recovered')),
            source_reference text NOT NULL,
            streak_count integer NOT NULL CHECK (streak_count >= 1),
            delivery_status text NOT NULL CHECK (delivery_status IN ('observed','pending','sent','failed','disabled')),
            message_text text NOT NULL DEFAULT '',
            response jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text,
            sent_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(channel,event_type,source_reference)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS alert_delivery_health_events_status_idx
        ON quant.alert_delivery_health_events(channel, delivery_status, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.alert_delivery_health_events")
