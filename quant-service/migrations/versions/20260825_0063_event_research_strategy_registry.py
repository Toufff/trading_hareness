"""Register the five full-market event-research studies.

Revision ID: 20260825_0063
Revises: 20260825_0062

These are descriptive cross-sectional/event studies over already-ingested
canonical daily bars (now ~512 trading days), not live strategies: each
persists into quant.strategy_experiments (the same table watchlist_main_wave/
countertrend_rebound already use for research) so findings are queryable and
reproducible instead of living only in a hand-written markdown doc.
"""

from alembic import op


revision = "20260825_0063"
down_revision = "20260825_0062"
branch_labels = None
depends_on = None

STUDIES = (
    ("event_research_limit_up_continuation_v1", "涨停延续全市场事件研究", "sql-cohort-study"),
    ("event_research_daily_volume_surge_v1", "日频放量全市场事件研究", "sql-cohort-study"),
    ("event_research_short_term_reversal_v1", "短期反转横截面研究", "sql-decile-study"),
    ("event_research_sector_flow_reversal_stock_v1", "板块资金流反转个股级复算", "sql-cohort-study"),
    ("event_research_post_close_backtest_v1", "盘后三路径全市场抽样回测", "python-sampled-backtest"),
)


def upgrade() -> None:
    for strategy_key, label, engine in STUDIES:
        op.execute(f"""
            INSERT INTO quant.strategy_registry(strategy_key,label,engine,version,configuration,status)
            VALUES('{strategy_key}','{label}','{engine}','research-v1',
                   jsonb_build_object('status','descriptive_only','live_effect','none'),'experimental')
            ON CONFLICT(strategy_key) DO UPDATE SET label=EXCLUDED.label,engine=EXCLUDED.engine,
              version=EXCLUDED.version,configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
        """)


def downgrade() -> None:
    for strategy_key, _label, _engine in STUDIES:
        op.execute(f"DELETE FROM quant.strategy_experiments WHERE strategy_key='{strategy_key}'")
        op.execute(f"DELETE FROM quant.strategy_registry WHERE strategy_key='{strategy_key}'")
