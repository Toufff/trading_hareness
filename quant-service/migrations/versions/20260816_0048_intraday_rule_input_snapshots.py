"""Freeze causal input bundles for future intraday rule replay.

Revision ID: 20260816_0048
Revises: 20260816_0047
"""

from alembic import op


revision = "20260816_0048"
down_revision = "20260816_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_rule_input_snapshots (
            rule_input_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scan_id uuid NOT NULL REFERENCES quant.intraday_scan_runs(scan_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
            observed_at timestamptz NOT NULL,
            model_version text NOT NULL,
            input_hash text NOT NULL,
            inputs jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(scan_id,symbol,model_version)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_rule_input_snapshot_time_idx
            ON quant.intraday_rule_input_snapshots(observed_at DESC, symbol)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_rule_input_snapshots")
