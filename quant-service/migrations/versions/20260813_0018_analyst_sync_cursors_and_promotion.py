"""Durable analyst sync cursors and one live-promotion registry.

Revision ID: 20260813_0018
Revises: 20260813_0017
"""

from alembic import op


revision = "20260813_0018"
down_revision = "20260813_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_sync_cursors (
            stream_key text NOT NULL,
            remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id) ON DELETE CASCADE,
            received_at timestamptz, message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            report_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(stream_key, remote_analyst_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_sync_cursors_updated_idx ON quant.analyst_sync_cursors(updated_at DESC)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_promotion_registry (
            promotion_key text PRIMARY KEY,
            methodology_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','eligible_for_review','approved','disabled','revoked')),
            approved_by text, approved_at timestamptz,
            max_live_weight numeric NOT NULL DEFAULT 0 CHECK (max_live_weight >= 0 AND max_live_weight <= 0.10),
            reason text NOT NULL DEFAULT '',
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((status = 'approved') = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
        )
    """)
    op.execute("""
        INSERT INTO quant.analyst_promotion_registry(promotion_key,methodology_version,status,max_live_weight,reason,evidence)
        VALUES('analyst_delta','sleeping-experts-fixed-share-v1','disabled',0,
               'P0 safety default: only an explicitly approved research version may supply a nonzero live prior.',
               '{"live_strategy_effect":"none"}'::jsonb)
        ON CONFLICT(promotion_key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_promotion_registry")
    op.execute("DROP TABLE IF EXISTS quant.analyst_sync_cursors")
