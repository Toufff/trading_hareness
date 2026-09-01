"""Add auditable legacy stock-brain import and personal journal storage.

Revision ID: 20260901_0080
Revises: 20260901_0079
"""

from alembic import op


revision = "20260901_0080"
down_revision = "20260901_0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.legacy_import_runs (
            import_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_system text NOT NULL,
            source_snapshot_key text NOT NULL,
            source_path text NOT NULL,
            source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            source_size_bytes bigint NOT NULL CHECK (source_size_bytes >= 0),
            status text NOT NULL CHECK (status IN ('running','completed','failed')),
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_text text,
            UNIQUE(source_system,source_snapshot_key)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.legacy_import_table_receipts (
            import_run_id uuid NOT NULL REFERENCES quant.legacy_import_runs(import_run_id) ON DELETE CASCADE,
            source_table text NOT NULL,
            classification text NOT NULL,
            status text NOT NULL CHECK (status IN ('pending','running','completed','failed','excluded')),
            source_row_count bigint NOT NULL DEFAULT 0,
            archived_row_count bigint NOT NULL DEFAULT 0,
            canonical_row_count bigint NOT NULL DEFAULT 0,
            skipped_row_count bigint NOT NULL DEFAULT 0,
            last_rowid bigint NOT NULL DEFAULT 0,
            payload_digest text,
            error_text text,
            started_at timestamptz,
            completed_at timestamptz,
            PRIMARY KEY(import_run_id,source_table)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.legacy_source_records (
            legacy_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_system text NOT NULL,
            source_table text NOT NULL,
            source_row_key text NOT NULL,
            classification text NOT NULL CHECK (classification IN ('durable_fact','research_evidence','archive_only')),
            decision_eligible boolean NOT NULL DEFAULT false,
            effective_at timestamptz,
            available_at timestamptz,
            payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
            payload jsonb NOT NULL,
            first_import_run_id uuid NOT NULL REFERENCES quant.legacy_import_runs(import_run_id) ON DELETE RESTRICT,
            last_seen_import_run_id uuid NOT NULL REFERENCES quant.legacy_import_runs(import_run_id) ON DELETE RESTRICT,
            first_imported_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(source_system,source_table,source_row_key,payload_sha256)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS legacy_source_records_lookup_idx
          ON quant.legacy_source_records(source_table,source_row_key,last_seen_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS legacy_source_records_time_idx
          ON quant.legacy_source_records(available_at DESC,effective_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.personal_journal_entries (
            journal_entry_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entry_date date NOT NULL,
            entry_type text NOT NULL CHECK (entry_type IN ('review','trade','plan','note')),
            title text NOT NULL,
            body text NOT NULL DEFAULT '',
            positions jsonb NOT NULL DEFAULT '[]'::jsonb,
            actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            plans jsonb NOT NULL DEFAULT '[]'::jsonb,
            watchlist jsonb NOT NULL DEFAULT '[]'::jsonb,
            discipline jsonb NOT NULL DEFAULT '{}'::jsonb,
            source text NOT NULL,
            source_record_key text NOT NULL,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(source,source_record_key,content_hash)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS personal_journal_entries_date_idx
          ON quant.personal_journal_entries(entry_date DESC,created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.personal_journal_entries")
    op.execute("DROP TABLE IF EXISTS quant.legacy_source_records")
    op.execute("DROP TABLE IF EXISTS quant.legacy_import_table_receipts")
    op.execute("DROP TABLE IF EXISTS quant.legacy_import_runs")
