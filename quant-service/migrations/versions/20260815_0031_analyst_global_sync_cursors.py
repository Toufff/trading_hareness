"""Persist opaque global cursors for remote analyst change feeds.

Revision ID: 20260815_0031
Revises: 20260815_0030
"""

from alembic import op


revision = "20260815_0031"
down_revision = "20260815_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_global_sync_cursors (
            stream_key text PRIMARY KEY CHECK (stream_key IN ('message_updates')),
            remote_cursor text,
            received_after timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_global_sync_cursors")
