"""Register the prior-session limit-up universe source.

Measured over 156 sessions: 20.20% of limit-up names touch the limit again the
next session against a 1.58% market rate.  Registered disabled at zero weight
like every other contract - and it is a universe source, not an entry signal:
the open-to-close edge is absent once the unbuyable overnight gap is removed.

Revision ID: 20260826_0070
Revises: 20260826_0069
"""

from alembic import op


revision = "20260826_0070"
down_revision = "20260826_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_promotion_registry(
            strategy_key,methodology_version,status,max_live_weight,reason,evidence)
        VALUES('limit_up_continuation','limit-up-continuation-v1','disabled',0,
               'P0 safety default: only an explicitly approved research version may supply a nonzero live weight.',
               '{"live_strategy_effect":"none","role":"universe_source_not_entry_signal"}'::jsonb)
        ON CONFLICT(strategy_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_promotion_registry WHERE strategy_key='limit_up_continuation'")
