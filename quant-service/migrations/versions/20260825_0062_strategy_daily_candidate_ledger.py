"""Unified cross-strategy candidate ledger, generic outcome settlement and daily regime persistence.

Revision ID: 20260825_0062
Revises: 20260825_0061

Every post-close/board-mining strategy scored candidates on its own scale in
its own table with no way to compare "today's best ideas across strategies".
This adds one normalized ledger any strategy can be materialized into, one
generic outcome table keyed the same way, and a persisted daily market-regime
label (previously computed on demand and never stored) so both can be
stratified by regime.
"""

from alembic import op


revision = "20260825_0062"
down_revision = "20260825_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_daily_candidates (
            strategy_key text NOT NULL,
            as_of_date date NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            source_table text NOT NULL,
            source_run_id uuid,
            rank integer,
            raw_score numeric,
            score_scale text NOT NULL,
            liquidity_eligible boolean NOT NULL DEFAULT true,
            liquidity_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            materialized_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(strategy_key, as_of_date, symbol)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS strategy_daily_candidates_date_idx ON quant.strategy_daily_candidates(as_of_date, strategy_key)")
    op.execute("CREATE INDEX IF NOT EXISTS strategy_daily_candidates_symbol_idx ON quant.strategy_daily_candidates(symbol, as_of_date DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_daily_candidate_outcomes (
            strategy_key text NOT NULL,
            as_of_date date NOT NULL,
            symbol text NOT NULL,
            entry_date date NOT NULL,
            horizon_days integer NOT NULL,
            entry_price numeric NOT NULL,
            exit_price numeric,
            raw_return numeric,
            benchmark_return numeric,
            excess_return numeric,
            maximum_favorable_excursion numeric,
            maximum_adverse_excursion numeric,
            tradability text NOT NULL DEFAULT 'unknown',
            calculated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(strategy_key, as_of_date, symbol),
            FOREIGN KEY(strategy_key, as_of_date, symbol)
                REFERENCES quant.strategy_daily_candidates(strategy_key, as_of_date, symbol) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS strategy_daily_candidate_outcomes_strategy_idx
            ON quant.strategy_daily_candidate_outcomes(strategy_key, entry_date DESC)
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.market_regime_daily (
            trading_date date PRIMARY KEY,
            model_version text NOT NULL,
            regime_label text NOT NULL,
            index_count integer NOT NULL DEFAULT 0,
            median_range_retracement numeric,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            calculated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.market_regime_daily")
    op.execute("DROP TABLE IF EXISTS quant.strategy_daily_candidate_outcomes")
    op.execute("DROP TABLE IF EXISTS quant.strategy_daily_candidates")
