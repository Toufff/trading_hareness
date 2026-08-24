"""Add bounded intraday evidence for the ten-day shadow cohort.

Revision ID: 20260824_0057
Revises: 20260823_0056
"""

from alembic import op


revision = "20260824_0057"
down_revision = "20260823_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.ten_day_leader_rotation_intraday_observations (
            observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid NOT NULL REFERENCES quant.ten_day_leader_rotation_runs(run_id) ON DELETE CASCADE,
            scan_id uuid NOT NULL REFERENCES quant.intraday_scan_runs(scan_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            observed_at timestamptz NOT NULL,
            quote_source text NOT NULL,
            shadow_state text NOT NULL,
            shadow_eligible boolean NOT NULL DEFAULT false,
            decision_eligible boolean NOT NULL DEFAULT false CHECK(NOT decision_eligible),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(run_id,scan_id,symbol)
        );
        CREATE INDEX IF NOT EXISTS ten_day_rotation_intraday_latest_idx
            ON quant.ten_day_leader_rotation_intraday_observations(run_id,symbol,observed_at DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.ten_day_leader_rotation_intraday_observations")
