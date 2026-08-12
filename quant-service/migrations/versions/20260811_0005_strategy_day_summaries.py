"""Persist one auditable post-close summary delivery per exchange date.

Revision ID: 20260811_0005
Revises: 20260811_0004
"""

from alembic import op


revision = "20260811_0005"
down_revision = "20260811_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_day_summaries (
            summary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            exchange_date date NOT NULL UNIQUE,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            message_text text NOT NULL DEFAULT '',
            delivery_status text NOT NULL DEFAULT 'pending'
                CHECK (delivery_status IN ('pending','sent','failed','disabled','suppressed')),
            attempt_count integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz,
            sent_at timestamptz,
            error_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS strategy_day_summaries_delivery_idx
        ON quant.strategy_day_summaries(delivery_status, next_attempt_at, exchange_date DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.strategy_day_summaries")
