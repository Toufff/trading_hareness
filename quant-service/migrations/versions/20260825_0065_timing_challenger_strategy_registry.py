"""Register the offline entry-timing challenger backtest.

Revision ID: 20260825_0065
Revises: 20260825_0064
"""

from alembic import op


revision = "20260825_0065"
down_revision = "20260825_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_registry(strategy_key,label,engine,version,configuration,status)
        VALUES('intraday_entry_timing_challengers_v1','盘中入场择时 challenger 对拍','python-recorded-input-replay','research-v1',
               jsonb_build_object('status','descriptive_only','live_effect','none'),'experimental')
        ON CONFLICT(strategy_key) DO UPDATE SET label=EXCLUDED.label,engine=EXCLUDED.engine,
          version=EXCLUDED.version,configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.strategy_experiments WHERE strategy_key='intraday_entry_timing_challengers_v1'")
    op.execute("DELETE FROM quant.strategy_registry WHERE strategy_key='intraday_entry_timing_challengers_v1'")
