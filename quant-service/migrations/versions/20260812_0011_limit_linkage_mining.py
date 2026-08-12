"""Persist live limit-up linkage research candidates.

Revision ID: 20260812_0011
Revises: 20260812_0010
"""

from alembic import op


revision = "20260812_0011"
down_revision = "20260812_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_limit_linkage_mining_runs (
            linkage_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            observed_at timestamptz NOT NULL,
            trade_date date NOT NULL,
            status text NOT NULL CHECK (status IN ('completed','partial','blocked','failed')),
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_limit_linkage_candidates (
            candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            linkage_run_id uuid NOT NULL REFERENCES quant.intraday_limit_linkage_mining_runs(linkage_run_id) ON DELETE CASCADE,
            rank integer NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            name text,
            score numeric NOT NULL,
            shared_concepts integer NOT NULL,
            concept_labels jsonb NOT NULL DEFAULT '[]'::jsonb,
            leader_symbols jsonb NOT NULL DEFAULT '[]'::jsonb,
            leader_names jsonb NOT NULL DEFAULT '[]'::jsonb,
            pct_change numeric,
            main_net_inflow numeric,
            volume_ratio numeric,
            turnover_rate numeric,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            UNIQUE(linkage_run_id,rank)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_limit_linkage_runs_observed ON quant.intraday_limit_linkage_mining_runs(observed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_limit_linkage_candidates_run ON quant.intraday_limit_linkage_candidates(linkage_run_id,rank)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_limit_linkage_candidates")
    op.execute("DROP TABLE IF EXISTS quant.intraday_limit_linkage_mining_runs")
