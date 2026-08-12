"""Coordinate conservative Tushare request pacing across service replicas.

Revision ID: 20260811_0008
Revises: 20260811_0007
"""

from alembic import op


revision = "20260811_0008"
down_revision = "20260811_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.provider_rate_limit_slots (
            provider_key text PRIMARY KEY,
            next_allowed_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.provider_rate_limit_slots")
