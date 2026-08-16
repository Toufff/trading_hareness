"""Register the bounded SQL factor strategy engine.

Revision ID: 20260816_0038
Revises: 20260816_0037
"""

from alembic import op


revision = "20260816_0038"
down_revision = "20260816_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE quant.strategy_registry
           SET engine='native_factor_sql_v2',version='strategy-v2',
               configuration=configuration || jsonb_build_object(
                   'point_in_time_universe',true,
                   'non_overlapping_periods',true,
                   'same_day_exit',false),
               updated_at=now()
         WHERE strategy_key='multi_factor_rank_v1'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE quant.strategy_registry
           SET engine='native-a-share-simulator',version='strategy-v1',
               configuration=configuration-'point_in_time_universe'-'non_overlapping_periods'-'same_day_exit',
               updated_at=now()
         WHERE strategy_key='multi_factor_rank_v1'
    """)
