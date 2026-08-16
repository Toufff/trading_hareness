"""Register the abstaining watchlist main-wave challenger.

Revision ID: 20260816_0040
Revises: 20260816_0039
"""

from alembic import op


revision = "20260816_0040"
down_revision = "20260816_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_registry(
            strategy_key,label,engine,version,configuration,status
        ) VALUES(
            'watchlist_main_wave_shadow_v2','观察池主升启动可空仓影子模型',
            'qlib-aligned-causal-pattern','research-v2',
            jsonb_build_object(
                'status','shadow_only','history_calendar_days',365,
                'lookback_trading_days',60,'horizon_trading_days',10,
                'entry','next_session_open',
                'selection','qualified_only_max_3_per_day_may_abstain',
                'test_reuse_policy','diagnostic_only',
                'no_feishu_alert',true,'no_automatic_order',true
            ),
            'experimental'
        ) ON CONFLICT(strategy_key) DO UPDATE SET
            label=EXCLUDED.label,engine=EXCLUDED.engine,version=EXCLUDED.version,
            configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_registry WHERE strategy_key='watchlist_main_wave_shadow_v2'")
