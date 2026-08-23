"""Keep prior verification evidence separate from later probe outcomes.

Revision ID: 20260822_0055
Revises: 20260822_0054
"""

from alembic import op


revision = "20260822_0055"
down_revision = "20260822_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE quant.provider_api_capabilities
           SET note=CASE metadata->>'last_observation'
                    WHEN 'failed' THEN 'Previously verified; the latest bounded observation failed. See provider health for the current error.'
                    WHEN 'empty' THEN 'Previously verified; the latest bounded observation was a valid empty response.'
                    ELSE note
                END,
               metadata=metadata || jsonb_build_object(
                   'last_observation_note', note,
                   'capability_note_semantics', 'availability_is_historical_verification; last_observation_is_latest_probe'
               ),
               last_checked_at=now()
         WHERE availability='verified'
           AND metadata->>'last_observation' IN ('failed','empty');
    """)


def downgrade() -> None:
    # Prior free-form notes cannot be reconstructed safely; leave audit state.
    pass
