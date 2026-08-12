"""Manual provenance controls and durable analyst research summaries.

Revision ID: 20260812_0016
Revises: 20260812_0015
"""

from alembic import op


revision = "20260812_0016"
down_revision = "20260812_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.analyst_opinion_outcomes ADD COLUMN IF NOT EXISTS volatility numeric")
    op.execute("ALTER TABLE quant.analyst_opinion_outcomes ADD COLUMN IF NOT EXISTS normalized_reward numeric")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_research_profiles (
            remote_analyst_id text PRIMARY KEY REFERENCES quant.remote_analysts(remote_analyst_id) ON DELETE CASCADE,
            independence_class text NOT NULL DEFAULT 'unknown'
                CHECK (independence_class IN ('unknown','independent','institutional','promotional')),
            audience_size integer CHECK (audience_size IS NULL OR audience_size >= 0),
            audience_as_of timestamptz,
            evidence text NOT NULL DEFAULT '',
            source text NOT NULL DEFAULT 'manual_review',
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_research_runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            as_of_date date NOT NULL,
            methodology_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','research_only','eligible_for_review')),
            result jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(as_of_date,methodology_version)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_research_runs_latest_idx ON quant.analyst_research_runs(as_of_date DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_research_runs")
    op.execute("DROP TABLE IF EXISTS quant.analyst_research_profiles")
