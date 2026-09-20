"""Durable account-scoped intraday advisory evidence.

Revision ID: 20260921_0110
Revises: 20260920_0109
"""

from alembic import op


revision = "20260921_0110"
down_revision = "20260920_0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_advisory_events (
            event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), event_key text NOT NULL UNIQUE,
            symbol text NOT NULL, name text NOT NULL DEFAULT '', event_kind text NOT NULL,
            direction text NOT NULL, severity text NOT NULL CHECK(severity IN ('medium','high')),
            observed_at timestamptz NOT NULL, scope_source text NOT NULL CHECK(scope_source IN ('holding','recommendation')),
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb, summary text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS intraday_advisory_events_time_idx
          ON quant.intraday_advisory_events(observed_at DESC,symbol)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_advisory_analysis_runs (
            analysis_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), provider text NOT NULL CHECK(provider IN ('deepseek','codex')),
            trigger_kind text NOT NULL, report_kind text, started_at timestamptz NOT NULL,
            completed_at timestamptz NOT NULL, status text NOT NULL CHECK(status IN ('completed','failed')),
            input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'), input_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            output_hash text CHECK(output_hash IS NULL OR output_hash ~ '^[0-9a-f]{64}$'),
            output jsonb NOT NULL DEFAULT '{}'::jsonb, error_message text, created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS intraday_advisory_analysis_time_idx
          ON quant.intraday_advisory_analysis_runs(provider,completed_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_advisory_deliveries (
            delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), idempotency_key text NOT NULL UNIQUE,
            delivery_kind text NOT NULL CHECK(delivery_kind IN ('signal','analysis')),
            event_id uuid REFERENCES quant.intraday_advisory_events(event_id) ON DELETE CASCADE,
            analysis_run_id uuid REFERENCES quant.intraday_advisory_analysis_runs(analysis_run_id) ON DELETE CASCADE,
            status text NOT NULL CHECK(status IN ('pending','sent','failed','disabled')),
            message_text text NOT NULL, attempt_count integer NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
            next_attempt_at timestamptz, response jsonb NOT NULL DEFAULT '{}'::jsonb, error_message text,
            sent_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK((event_id IS NOT NULL)::integer + (analysis_run_id IS NOT NULL)::integer = 1)
        );
        CREATE INDEX IF NOT EXISTS intraday_advisory_delivery_due_idx
          ON quant.intraday_advisory_deliveries(next_attempt_at,created_at) WHERE status IN ('pending','failed')
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_advisory_runtime_status (
            runtime_key text PRIMARY KEY CHECK(runtime_key='primary'), state text NOT NULL,
            account_key text NOT NULL, last_tick_at timestamptz, scope_size integer NOT NULL DEFAULT 0,
            last_error text, details jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_advisory_runtime_status")
    op.execute("DROP TABLE IF EXISTS quant.intraday_advisory_deliveries")
    op.execute("DROP TABLE IF EXISTS quant.intraday_advisory_analysis_runs")
    op.execute("DROP TABLE IF EXISTS quant.intraday_advisory_events")
