"""Enable governed alert-only use of the rebound challenger.

Revision ID: 20260816_0042
Revises: 20260816_0041
"""

from alembic import op


revision = "20260816_0042"
down_revision = "20260816_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE quant.strategy_registry
           SET configuration=configuration || jsonb_build_object(
                 'live_effect','explicit_watchlist_research_alert_only',
                 'alert_eligible',true,
                 'probability_contract','shrunk_research_probability_with_effective_trading_days',
                 'no_feishu_alert',false,
                 'no_automatic_order',true
               ),
               updated_at=now()
         WHERE strategy_key='watchlist_countertrend_rebound_shadow_v1'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE quant.strategy_registry
           SET configuration=(configuration - 'live_effect' - 'alert_eligible' - 'probability_contract')
                 || jsonb_build_object('no_feishu_alert',true,'no_automatic_order',true),
               updated_at=now()
         WHERE strategy_key='watchlist_countertrend_rebound_shadow_v1'
    """)
