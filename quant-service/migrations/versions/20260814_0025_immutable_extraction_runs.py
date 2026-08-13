"""Allow immutable extraction attempts for the same source version.

Revision ID: 20260814_0025
Revises: 20260814_0024
"""

from alembic import op


revision = "20260814_0025"
down_revision = "20260814_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE quant.analyst_extraction_runs "
        "DROP CONSTRAINT IF EXISTS analyst_extraction_runs_source_kind_source_id_source_versio_key"
    )
    # 0024 was deployed once with a duplicate JSON key.  Repair the persisted
    # contracts without changing their research-only status.
    op.execute(
        """UPDATE quant.strategy_contracts
           SET contract = contract - 'label' || jsonb_build_object(
             'label_text', COALESCE(contract->>'label', strategy_key),
             'label_spec', jsonb_build_object(
               'fixed_horizons', jsonb_build_array('5m','15m','30m','close','next_close'),
               'triple_barrier', 'preregistered'))
         WHERE status='research_only' AND contract ? 'label'"""
    )


def downgrade() -> None:
    # Do not destroy duplicate immutable attempts on downgrade.
    pass
