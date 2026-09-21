"""Preserve legitimate negative broker average costs.

Revision ID: 20260921_0114
Revises: 20260921_0113
"""

from alembic import op


revision = "20260921_0114"
down_revision = "20260921_0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.broker_position_snapshots
        DROP CONSTRAINT IF EXISTS broker_position_snapshots_average_cost_check
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE quant.broker_position_snapshots
        ADD CONSTRAINT broker_position_snapshots_average_cost_check
        CHECK (average_cost IS NULL OR average_cost >= 0) NOT VALID
    """)
