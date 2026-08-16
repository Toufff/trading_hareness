"""Record bounded remote analyst sync liveness separately from cursors.

Revision ID: 20260816_0046
Revises: 20260816_0045
"""

from alembic import op


revision = "20260816_0046"
down_revision = "20260816_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_sync_attempts (
            attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            stream_key text NOT NULL CHECK (stream_key IN ('reports','messages')),
            status text NOT NULL CHECK (status IN ('completed','failed')),
            started_at timestamptz NOT NULL,
            completed_at timestamptz NOT NULL,
            error_code text,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS analyst_sync_attempts_stream_completed_idx
            ON quant.analyst_sync_attempts(stream_key,completed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_sync_attempts")
