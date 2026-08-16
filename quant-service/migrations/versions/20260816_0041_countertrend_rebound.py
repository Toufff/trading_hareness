"""Register the watchlist counter-trend rebound shadow strategy.

Revision ID: 20260816_0041
Revises: 20260816_0040
"""

from alembic import op


revision = "20260816_0041"
down_revision = "20260816_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_registry(
            strategy_key,label,engine,version,configuration,status
        ) VALUES(
            'watchlist_countertrend_rebound_shadow_v1','科技下跌浪与B浪反弹影子策略',
            'causal-countertrend-state-machine','research-v1',
            jsonb_build_object(
                'status','shadow_only','history_calendar_days',365,
                'lookback_trading_days',60,'horizon_trading_days',5,
                'entry','next_session_open','selection','confirmed_only_max_5_per_day',
                'panic_policy','observation_only_never_direct_entry',
                'no_feishu_alert',true,'no_automatic_order',true
            ),
            'experimental'
        ) ON CONFLICT(strategy_key) DO UPDATE SET
            label=EXCLUDED.label,engine=EXCLUDED.engine,version=EXCLUDED.version,
            configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_registry WHERE strategy_key='watchlist_countertrend_rebound_shadow_v1'")
