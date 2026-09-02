"""Register the SQL factor-lab evaluator so its trials can be ledgered.

Revision ID: 20260902_0087
Revises: 20260902_0086

``quant.strategy_experiments`` is the existing cumulative trial ledger (its
``strategy_key`` is a foreign key into ``quant.strategy_registry``), already
used by the timing-challenger and multi-factor-strategy backtests.  The
per-factor SQL evaluator (``factor_sql_lab.evaluate_factor_set``) never wrote
into it, so its deflated-Sharpe multiple-comparison correction had no
honestly-counted trial count and shipped hardcoded ``None``.  This registers
one strategy_key per evaluable SQL factor so each factor's own trial count
can be read back from ``strategy_experiments`` before every new evaluation.
"""

from alembic import op


revision = "20260902_0087"
down_revision = "20260902_0086"
branch_labels = None
depends_on = None

# Keep in sync with app.factor_sql_lab.SQL_FACTOR_COLUMNS.
_FACTOR_KEYS = (
    "momentum_5d", "momentum_20d", "reversal_5d", "sma_gap_20d",
    "volatility_20d", "volume_ratio_20d", "intraday_strength",
)


def upgrade() -> None:
    for factor_key in _FACTOR_KEYS:
        strategy_key = f"sql_factor_lab:{factor_key}"
        op.execute(f"""
            INSERT INTO quant.strategy_registry(strategy_key,label,engine,version,configuration,status)
            VALUES('{strategy_key}','SQL 因子研究室 - {factor_key}','native-sql-cross-section','factor-lab-v2',
                   jsonb_build_object('factor_key','{factor_key}','live_effect','none'),'experimental')
            ON CONFLICT(strategy_key) DO UPDATE SET label=EXCLUDED.label,engine=EXCLUDED.engine,
              version=EXCLUDED.version,configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
        """)


def downgrade() -> None:
    for factor_key in _FACTOR_KEYS:
        strategy_key = f"sql_factor_lab:{factor_key}"
        op.execute(f"DELETE FROM quant.strategy_experiments WHERE strategy_key='{strategy_key}'")
        op.execute(f"DELETE FROM quant.strategy_registry WHERE strategy_key='{strategy_key}'")
