"""Retain bounded settlement evidence for analyst intraday outcomes.

Revision ID: 20260816_0044
Revises: 20260816_0043
"""

from alembic import op


revision = "20260816_0044"
down_revision = "20260816_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.analyst_intraday_outcomes
        ADD COLUMN IF NOT EXISTS settlement jsonb NOT NULL DEFAULT '{}'::jsonb
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS analyst_intraday_outcomes_methodology_status_idx
        ON quant.analyst_intraday_outcomes(methodology_version,status,calculated_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.analyst_intraday_outcomes_methodology_status_idx")
    op.execute("ALTER TABLE quant.analyst_intraday_outcomes DROP COLUMN IF EXISTS settlement")
