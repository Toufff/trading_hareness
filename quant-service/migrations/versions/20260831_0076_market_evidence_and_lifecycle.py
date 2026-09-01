"""Persist scan rejection evidence and provider lifecycle snapshots.

Revision ID: 20260831_0076
Revises: 20260831_0075
"""

from alembic import op


revision = "20260831_0076"
down_revision = "20260831_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_scan_rejections (
            scan_id uuid NOT NULL REFERENCES quant.intraday_scan_runs(scan_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
            model_version text NOT NULL,
            observed_at timestamptz NOT NULL,
            outcome text NOT NULL CHECK (outcome IN ('rejected','candidate','suppressed')),
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(scan_id,symbol,model_version)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_scan_rejections_symbol_time_idx
          ON quant.intraday_scan_rejections(symbol,observed_at DESC,outcome)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.instrument_lifecycle_evidence (
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
            provider text NOT NULL,
            observed_at timestamptz NOT NULL,
            status_date date NOT NULL,
            list_status text NOT NULL CHECK (list_status IN ('L','D','P','UNKNOWN')),
            list_date date,
            delist_date date,
            is_st boolean,
            available_at timestamptz NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(symbol,provider,status_date,list_status)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS instrument_lifecycle_status_date_idx
          ON quant.instrument_lifecycle_evidence(list_status,observed_at DESC,symbol)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.instrument_lifecycle_evidence")
    op.execute("DROP TABLE IF EXISTS quant.intraday_scan_rejections")
