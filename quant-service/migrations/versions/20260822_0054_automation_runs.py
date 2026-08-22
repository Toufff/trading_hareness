"""Add a durable run ledger for scheduled research tasks.

Revision ID: 20260822_0054
Revises: 20260822_0053
"""

from alembic import op

revision = "20260822_0054"
down_revision = "20260822_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.automation_runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            task_key text NOT NULL,
            run_key text NOT NULL UNIQUE,
            cadence text,
            as_of_date date,
            status text NOT NULL CHECK (status IN ('queued','running','completed','partial','failed','blocked')),
            methodology_version text,
            input_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            output_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_class text,
            error_message text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS automation_runs_task_time_idx
            ON quant.automation_runs(task_key, started_at DESC);
        CREATE INDEX IF NOT EXISTS automation_runs_status_time_idx
            ON quant.automation_runs(status, updated_at DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.automation_runs")
