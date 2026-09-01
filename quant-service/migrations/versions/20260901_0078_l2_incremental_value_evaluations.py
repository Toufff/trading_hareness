"""Persist the Level-2 incremental-value research gate.

Revision ID: 20260901_0078
Revises: 20260831_0077
"""

from alembic import op


revision = "20260901_0078"
down_revision = "20260831_0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.l2_incremental_value_evaluations (
            evaluation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            evaluated_at timestamptz NOT NULL DEFAULT now(),
            source_kind text NOT NULL CHECK (source_kind='licensed_level2_offline'),
            algorithm_version text NOT NULL,
            minimum_samples integer NOT NULL CHECK (minimum_samples > 0),
            samples integer NOT NULL CHECK (samples >= 0),
            mean_incremental_value numeric,
            ci95_lower numeric,
            status text NOT NULL CHECK (status IN ('blocked','eligible_for_research_expansion')),
            l2_algorithm_versions jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            live_effect text NOT NULL DEFAULT 'none' CHECK (live_effect='none')
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS l2_incremental_value_evaluations_time_idx
          ON quant.l2_incremental_value_evaluations(evaluated_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.l2_incremental_value_evaluations")
