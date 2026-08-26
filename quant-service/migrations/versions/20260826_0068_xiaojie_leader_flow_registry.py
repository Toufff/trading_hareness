"""Register the Xiao Jie leader-flow research strategy with zero live weight.

Revision ID: 20260826_0068
Revises: 20260826_0067
"""

from alembic import op


revision = "20260826_0068"
down_revision = "20260826_0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_promotion_registry(
            strategy_key,methodology_version,status,max_live_weight,reason,evidence)
        VALUES('xiaojie_leader_flow','xiaojie-leader-flow-v1','disabled',0,
               'P0 safety default: research-only Xiao Jie leader-flow quantification; explicit approval required.',
               '{"live_strategy_effect":"none","boundary":"research_only"}'::jsonb)
        ON CONFLICT(strategy_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_promotion_registry WHERE strategy_key='xiaojie_leader_flow'")
