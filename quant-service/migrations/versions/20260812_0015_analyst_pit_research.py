"""Point-in-time analyst research ledger and sleeping-expert aggregates.

Revision ID: 20260812_0015
Revises: 20260812_0014
"""

from alembic import op


revision = "20260812_0015"
down_revision = "20260812_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.remote_reports ADD COLUMN IF NOT EXISTS remote_published_at timestamptz")
    op.execute("ALTER TABLE quant.analyst_claims ADD COLUMN IF NOT EXISTS published_at timestamptz")
    op.execute("ALTER TABLE quant.analyst_claims ADD COLUMN IF NOT EXISTS explicitness numeric NOT NULL DEFAULT 0.5")
    # Idempotent: a partially applied or manually repaired database may already
    # carry the constraint, and ``ADD CONSTRAINT`` has no ``IF NOT EXISTS``.
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint
                            WHERE conname='analyst_claims_explicitness_range'
                              AND conrelid='quant.analyst_claims'::regclass) THEN
                ALTER TABLE quant.analyst_claims ADD CONSTRAINT analyst_claims_explicitness_range
                    CHECK (explicitness >= 0 AND explicitness <= 1) NOT VALID;
            END IF;
        END $$
    """)
    op.execute("""
        UPDATE quant.remote_reports
           SET remote_published_at=coalesce(remote_published_at,remote_updated_at,remote_created_at)
         WHERE remote_published_at IS NULL
    """)
    # Historical reports are safe only from the moment this service first stored
    # them.  Preserve remote publication separately for latency diagnostics.
    op.execute("""
        UPDATE quant.analyst_evidence e
           SET available_at=r.first_synced_at
          FROM quant.remote_reports r
         WHERE e.remote_report_id=r.remote_report_id
           AND e.available_at IS DISTINCT FROM r.first_synced_at
    """)
    op.execute("""
        UPDATE quant.analyst_claims c
           SET available_at=r.first_synced_at,
               published_at=coalesce(c.published_at,r.remote_published_at,r.remote_updated_at,r.remote_created_at),
               explicitness=coalesce(c.explicitness,0.5)
          FROM quant.analyst_evidence e
          JOIN quant.remote_reports r ON r.remote_report_id=e.remote_report_id
         WHERE c.evidence_id=e.evidence_id
    """)
    op.execute("""
        UPDATE quant.analyst_trade_actions a
           SET available_at=r.first_synced_at
          FROM quant.remote_reports r
         WHERE a.remote_report_id=r.remote_report_id
           AND a.available_at IS DISTINCT FROM r.first_synced_at
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_theme_board_aliases (
            theme_key text NOT NULL,
            theme_label text NOT NULL,
            taxonomy_key text NOT NULL,
            sector_key text NOT NULL,
            mapping_method text NOT NULL CHECK (mapping_method IN ('exact_label','reviewed_alias')),
            status text NOT NULL DEFAULT 'approved' CHECK (status IN ('approved','pending','rejected')),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(theme_key,taxonomy_key,sector_key),
            FOREIGN KEY(taxonomy_key,sector_key) REFERENCES quant.sectors(taxonomy_key,sector_key) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_opinions (
            opinion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id) ON DELETE CASCADE,
            opinion_date date NOT NULL,
            scope text NOT NULL CHECK (scope IN ('market','theme','stock')),
            subject_key text NOT NULL,
            subject_label text NOT NULL,
            direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
            strength numeric NOT NULL CHECK (strength >= 0 AND strength <= 1),
            explicitness numeric NOT NULL CHECK (explicitness >= 0 AND explicitness <= 1),
            horizon_days integer NOT NULL,
            published_at timestamptz,
            available_at timestamptz NOT NULL,
            latency_seconds integer,
            factor_status text NOT NULL CHECK (factor_status IN ('eligible','replay_only','unmapped','neutral')),
            source_claim_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence_count integer NOT NULL DEFAULT 0,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(remote_analyst_id,opinion_date,scope,subject_key,horizon_days)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_opinions_pit_idx ON quant.analyst_opinions(available_at,scope,subject_key)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_opinion_outcomes (
            outcome_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            opinion_id uuid NOT NULL REFERENCES quant.analyst_opinions(opinion_id) ON DELETE CASCADE,
            horizon_days integer NOT NULL,
            entry_date date,
            exit_date date,
            basket_size integer NOT NULL DEFAULT 0,
            raw_return numeric,
            benchmark_return numeric,
            residual_return numeric,
            directional_return numeric,
            status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
            methodology_version text NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            settled_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(opinion_id,horizon_days,methodology_version)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_opinion_outcomes_status_idx ON quant.analyst_opinion_outcomes(status,horizon_days)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_expert_runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            as_of_date date NOT NULL,
            model_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','research_only','eligible_for_review')),
            result jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(as_of_date,model_version)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_expert_runs")
    op.execute("DROP TABLE IF EXISTS quant.analyst_opinion_outcomes")
    op.execute("DROP TABLE IF EXISTS quant.analyst_opinions")
    op.execute("DROP TABLE IF EXISTS quant.analyst_theme_board_aliases")
    op.execute("ALTER TABLE quant.analyst_claims DROP CONSTRAINT IF EXISTS analyst_claims_explicitness_range")
    op.execute("ALTER TABLE quant.analyst_claims DROP COLUMN IF EXISTS explicitness")
    op.execute("ALTER TABLE quant.analyst_claims DROP COLUMN IF EXISTS published_at")
    op.execute("ALTER TABLE quant.remote_reports DROP COLUMN IF EXISTS remote_published_at")
