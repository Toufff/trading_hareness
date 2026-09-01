"""Add terminal decision research dossiers and gate evidence.

Revision ID: 20260901_0083
Revises: 20260901_0082
"""

from alembic import op


revision = "20260901_0083"
down_revision = "20260901_0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.decision_research_dossiers (
            dossier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            dossier_key text NOT NULL UNIQUE,
            as_of_date date NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            name text NOT NULL,
            strategy_family text NOT NULL,
            model_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('passed','rejected','incomplete')),
            conclusion text NOT NULL,
            source_candidate_run_id uuid REFERENCES quant.post_close_strategy_runs(run_id) ON DELETE SET NULL,
            source_candidate_rank integer,
            evidence_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS decision_research_dossiers_latest_idx
          ON quant.decision_research_dossiers(symbol,as_of_date DESC,created_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.decision_research_gates (
            dossier_id uuid NOT NULL REFERENCES quant.decision_research_dossiers(dossier_id) ON DELETE CASCADE,
            gate_key text NOT NULL CHECK (gate_key ~ '^G[0-7]$'),
            label text NOT NULL,
            verdict text NOT NULL CHECK (verdict IN ('pass','fail','unknown','advisory')),
            independent_run boolean NOT NULL DEFAULT false,
            conclusion text NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            completed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(dossier_id,gate_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS decision_research_gates_verdict_idx
          ON quant.decision_research_gates(gate_key,verdict,completed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.decision_research_gates")
    op.execute("DROP TABLE IF EXISTS quant.decision_research_dossiers")
