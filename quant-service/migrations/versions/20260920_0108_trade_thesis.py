"""Persist immutable trade-thesis revisions, evaluations, and plan bindings.

Revision ID: 20260920_0108
Revises: 20260920_0107
"""

from alembic import op


revision = "20260920_0108"
down_revision = "20260920_0107"
branch_labels = None
depends_on = None

INDEXES = (
    ("trade_thesis_evaluations_tier_cutoff_idx", "quant.trade_thesis_evaluations", "created_at"),
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE quant.trade_thesis_revisions (
            event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_seq bigserial NOT NULL UNIQUE,
            thesis_id text NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            event_type text NOT NULL CHECK (event_type IN
                ('capture','proposal','approve','reject','needs_evidence')),
            content_revision integer NOT NULL CHECK (content_revision >= 1),
            base_revision integer,
            payload jsonb NOT NULL,
            actor text NOT NULL,
            proposal_hash text,
            review_data jsonb NOT NULL DEFAULT '{}'::jsonb,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(thesis_id,event_type,content_hash),
            UNIQUE(thesis_id,event_type,content_revision)
        )
    """)
    op.execute("""
        CREATE INDEX trade_thesis_revisions_symbol_time_idx
            ON quant.trade_thesis_revisions(symbol,created_at DESC)
    """)
    op.execute("""
        CREATE TABLE quant.trade_thesis_evaluations (
            evaluation_id text NOT NULL,
            thesis_id text NOT NULL,
            thesis_revision integer NOT NULL CHECK (thesis_revision >= 1),
            source_run_id text NOT NULL,
            cutoff_at timestamptz NOT NULL,
            namespace text NOT NULL CHECK (namespace IN ('capture','shadow','advisory')),
            thesis_state text NOT NULL,
            evidence_status text NOT NULL,
            entry_state text NOT NULL,
            input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
            result jsonb NOT NULL,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(evaluation_id,namespace),
            UNIQUE(thesis_id,source_run_id,cutoff_at,namespace,content_hash)
        )
    """)
    op.execute("""
        CREATE INDEX trade_thesis_evaluations_lookup_idx
            ON quant.trade_thesis_evaluations(thesis_id,cutoff_at DESC,created_at DESC)
    """)
    op.execute("""
        CREATE INDEX trade_thesis_evaluations_state_idx
            ON quant.trade_thesis_evaluations(thesis_state,evidence_status,entry_state,cutoff_at DESC)
    """)
    op.execute("""
        CREATE INDEX trade_thesis_evaluations_tier_cutoff_idx
            ON quant.trade_thesis_evaluations(created_at)
    """)
    op.execute("""
        CREATE TABLE quant.plan_thesis_bindings (
            binding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            position_episode_id text,
            thesis_id text NOT NULL,
            thesis_revision integer NOT NULL CHECK (thesis_revision >= 1),
            plan_id text,
            binding_source text NOT NULL,
            bound_at timestamptz NOT NULL,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            content_hash text NOT NULL UNIQUE CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX plan_thesis_bindings_lookup_idx
            ON quant.plan_thesis_bindings(account_key,symbol,bound_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.plan_thesis_bindings")
    op.execute("DROP TABLE IF EXISTS quant.trade_thesis_evaluations")
    op.execute("DROP TABLE IF EXISTS quant.trade_thesis_revisions")
