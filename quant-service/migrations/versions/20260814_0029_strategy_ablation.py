"""Persist market-only versus analyst-shadow recommendation evidence.

Revision ID: 20260814_0029
Revises: 20260814_0028
"""

from alembic import op


revision = "20260814_0029"
down_revision = "20260814_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_ablation_observations (
            ablation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES quant.recommendation_runs(run_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            market_only_score numeric NOT NULL,
            analyst_shadow_score numeric NOT NULL,
            applied_score numeric NOT NULL,
            market_signal numeric NOT NULL,
            analyst_signal numeric,
            analyst_delta numeric NOT NULL,
            applied_analyst_weight numeric NOT NULL CHECK (applied_analyst_weight >= 0 AND applied_analyst_weight <= 0.10),
            analyst_execution_status text NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(run_id,symbol)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS strategy_ablation_time_idx ON quant.strategy_ablation_observations(created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.strategy_ablation_observations")
