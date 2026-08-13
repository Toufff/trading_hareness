"""Append-only unified analyst observation and extraction ledgers.

Revision ID: 20260814_0022
Revises: 20260814_0021
"""

from alembic import op


revision = "20260814_0022"
down_revision = "20260814_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_extraction_runs (
            extraction_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
            source_kind text NOT NULL CHECK (source_kind IN ('report','message')),
            source_id text NOT NULL,
            source_version text NOT NULL,
            content_hash text NOT NULL,
            extractor_version text NOT NULL,
            schema_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('completed','partial','failed','replay_only')),
            candidate_count integer NOT NULL DEFAULT 0,
            accepted_count integer NOT NULL DEFAULT 0,
            uncertainty jsonb NOT NULL DEFAULT '{}'::jsonb,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            UNIQUE(source_kind,source_id,source_version,content_hash,extractor_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_observations (
            observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            extraction_run_id uuid NOT NULL REFERENCES quant.analyst_extraction_runs(extraction_run_id),
            analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
            source_kind text NOT NULL CHECK (source_kind IN ('report','message')),
            source_id text NOT NULL,
            source_version text NOT NULL,
            content_hash text NOT NULL,
            received_at timestamptz NOT NULL,
            strategy_available_at timestamptz NOT NULL,
            published_at timestamptz,
            edited_at timestamptz,
            stated_at timestamptz,
            stated_precision text CHECK (stated_precision IN ('minute','second')),
            scope text NOT NULL CHECK (scope IN ('market','theme','stock')),
            subject_key text NOT NULL,
            subject_label text NOT NULL DEFAULT '',
            action text NOT NULL CHECK (action IN ('buy','watch','reduce','sell','avoid','neutral','mention')),
            direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
            horizon_days integer,
            strength numeric,
            confidence numeric,
            position_intent text,
            conditions jsonb NOT NULL DEFAULT '{}'::jsonb,
            evidence_span text NOT NULL DEFAULT '',
            extractor_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('eligible','replay_only','neutral','unmapped','rejected')),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(source_kind,source_id,source_version,content_hash,scope,subject_key,horizon_days,extractor_version)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_observations_pit_idx ON quant.analyst_observations(analyst_id,strategy_available_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS analyst_observations_subject_idx ON quant.analyst_observations(scope,subject_key,strategy_available_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS analyst_extraction_runs_time_idx ON quant.analyst_extraction_runs(finished_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_observations")
    op.execute("DROP TABLE IF EXISTS quant.analyst_extraction_runs")
