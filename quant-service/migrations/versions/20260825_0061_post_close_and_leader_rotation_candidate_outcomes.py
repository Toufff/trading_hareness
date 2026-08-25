"""Outcome tables for the post-close and ten-day leader rotation candidate lines.

Revision ID: 20260825_0061
Revises: 20260825_0060
"""

from alembic import op


revision = "20260825_0061"
down_revision = "20260825_0060"
branch_labels = None
depends_on = None

_TABLES = ("post_close_strategy_candidate_outcomes", "ten_day_leader_rotation_candidate_outcomes")
_RUN_FK = {
    "post_close_strategy_candidate_outcomes": "post_close_strategy_runs",
    "ten_day_leader_rotation_candidate_outcomes": "ten_day_leader_rotation_runs",
}


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            CREATE TABLE IF NOT EXISTS quant.{table} (
                run_id uuid NOT NULL REFERENCES quant.{_RUN_FK[table]}(run_id) ON DELETE CASCADE,
                symbol text NOT NULL REFERENCES quant.instruments(symbol),
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
                PRIMARY KEY(run_id, symbol)
            )
        """)
        op.execute(f"CREATE INDEX IF NOT EXISTS {table}_symbol_idx ON quant.{table}(symbol, entry_date DESC)")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS quant.{table}")
