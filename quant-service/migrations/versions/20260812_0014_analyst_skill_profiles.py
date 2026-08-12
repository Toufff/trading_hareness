"""Persist offline analyst-language distillation profiles.

Revision ID: 20260812_0014
Revises: 20260812_0013
"""

from alembic import op


revision = "20260812_0014"
down_revision = "20260812_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_skill_profiles (
            profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id) ON DELETE CASCADE,
            as_of_date date NOT NULL,
            model_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','reviewable','approved','rejected')),
            profile jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(remote_analyst_id,as_of_date,model_version)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_skill_profiles_latest_idx ON quant.analyst_skill_profiles(remote_analyst_id,as_of_date DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_skill_profiles")
