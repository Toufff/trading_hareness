"""Add immutable analyst Prompt-Lab facts and local intraday outcome ledger.

Both ledgers are research-only.  They deliberately have no foreign key into a
live strategy configuration, so a prompt experiment cannot change trading
behaviour by itself.

Revision ID: 20260814_0028
Revises: 20260814_0027
"""

from alembic import op


revision = "20260814_0028"
down_revision = "20260814_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_prompt_candidates (
            candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            observation_id uuid NOT NULL REFERENCES quant.analyst_observations(observation_id) ON DELETE CASCADE,
            analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
            variant_key text NOT NULL,
            variant_version text NOT NULL,
            candidate_hash text NOT NULL,
            payload jsonb NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','labelled','evaluated','rejected')) DEFAULT 'collecting',
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(observation_id,variant_key,variant_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_prompt_gold_labels (
            candidate_id uuid PRIMARY KEY REFERENCES quant.analyst_prompt_candidates(candidate_id) ON DELETE CASCADE,
            label text NOT NULL CHECK (label IN ('supported','unsupported','ambiguous')),
            direction_correct boolean,
            action_executable boolean,
            reviewer text NOT NULL,
            notes text NOT NULL DEFAULT '',
            labelled_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_prompt_evaluation_runs (
            evaluation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            variant_key text NOT NULL,
            variant_version text NOT NULL,
            cutoff_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','insufficient_labels','completed')),
            sample_count integer NOT NULL DEFAULT 0,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(variant_key,variant_version,cutoff_at)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_intraday_outcomes (
            observation_id uuid NOT NULL REFERENCES quant.analyst_observations(observation_id) ON DELETE CASCADE,
            methodology_version text NOT NULL,
            horizon_minutes integer NOT NULL CHECK (horizon_minutes IN (5,15,30,60)),
            status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
            entry_at timestamptz,
            entry_price numeric,
            exit_at timestamptz,
            exit_price numeric,
            directional_return numeric,
            source_name text,
            calculated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(observation_id,methodology_version,horizon_minutes)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_prompt_candidates_variant_idx ON quant.analyst_prompt_candidates(variant_key,created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS analyst_intraday_outcomes_status_idx ON quant.analyst_intraday_outcomes(status,calculated_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_intraday_outcomes")
    op.execute("DROP TABLE IF EXISTS quant.analyst_prompt_evaluation_runs")
    op.execute("DROP TABLE IF EXISTS quant.analyst_prompt_gold_labels")
    op.execute("DROP TABLE IF EXISTS quant.analyst_prompt_candidates")
