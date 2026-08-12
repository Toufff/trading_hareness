"""Persist confirmed one-minute board-flow rotation alerts.

Revision ID: 20260811_0007
Revises: 20260811_0006
"""

from alembic import op


revision = "20260811_0007"
down_revision = "20260811_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_board_rotation_events (
            rotation_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            snapshot_minute timestamptz NOT NULL,
            event_key text NOT NULL,
            taxonomy_key text NOT NULL,
            sector_key text NOT NULL,
            label text NOT NULL,
            event_type text NOT NULL CHECK (event_type IN ('cross_zero','flow_surge')),
            direction text NOT NULL CHECK (direction IN ('inflow','outflow')),
            state text NOT NULL CHECK (state IN ('confirming','confirmed','alerted','suppressed','expired')),
            first_observed_at timestamptz NOT NULL,
            last_observed_at timestamptz NOT NULL,
            confirmation_deadline timestamptz NOT NULL,
            conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS intraday_board_rotation_event_key_idx ON quant.intraday_board_rotation_events(event_key, last_observed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS intraday_board_rotation_state_idx ON quant.intraday_board_rotation_events(state, confirmation_deadline, last_observed_at DESC)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_board_rotation_deliveries (
            delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            rotation_event_id uuid NOT NULL REFERENCES quant.intraday_board_rotation_events(rotation_event_id) ON DELETE CASCADE,
            channel text NOT NULL,
            status text NOT NULL CHECK (status IN ('disabled','pending','sent','failed','suppressed')),
            response jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text,
            message_text text NOT NULL DEFAULT '',
            attempt_count integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz,
            sent_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(rotation_event_id, channel)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_board_rotation_delivery_retry_idx
        ON quant.intraday_board_rotation_deliveries(status, next_attempt_at, created_at)
        WHERE status IN ('pending','failed')
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_board_rotation_deliveries")
    op.execute("DROP TABLE IF EXISTS quant.intraday_board_rotation_events")
