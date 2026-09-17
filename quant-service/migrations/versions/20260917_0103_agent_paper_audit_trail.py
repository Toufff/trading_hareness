"""Audit trail for agent paper decisions: the exact input, the model transcript, order outcomes.

Revision ID: 20260917_0103
Revises: 20260917_0102

Only a hash and a length of the prompt context were kept, so a decision's
stated reasons could not be checked against what the agent actually saw, and
orders rejected before reaching the ledger left no row.  Columns are nullable:
earlier decisions simply have no trail.
"""

from alembic import op


revision = "20260917_0103"
down_revision = "20260917_0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.agent_paper_decisions
            ADD COLUMN IF NOT EXISTS context jsonb,
            ADD COLUMN IF NOT EXISTS transcript jsonb,
            ADD COLUMN IF NOT EXISTS outcomes jsonb
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE quant.agent_paper_decisions
            DROP COLUMN IF EXISTS outcomes,
            DROP COLUMN IF EXISTS transcript,
            DROP COLUMN IF EXISTS context
    """)
