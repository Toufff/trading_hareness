"""Machine-derived trade discipline: plans, evaluations, compliance, reviews.

Revision ID: 20260918_0105
Revises: 20260917_0104

Every table here is append-only research evidence for a human decision.  A plan
is never edited in place: a changed plan is a new row carrying
``supersedes_plan_id``, and a plan that fails the quality gate is stored with
``status='rejected_by_quality'`` rather than dropped, so the gate's own history
is auditable.  ``content_hash`` makes a re-run idempotent instead of appending a
duplicate; the same content under an existing key returns the stored row, and
different content under that key is a conflict the caller must resolve.

Nothing here is an order path and nothing replaces ``personal_trade_plans``.
"""

from alembic import op


revision = "20260918_0105"
down_revision = "20260917_0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_generation_runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            as_of_at timestamptz NOT NULL,
            trading_date date NOT NULL,
            generator_version text NOT NULL,
            inputs_hash text NOT NULL CHECK (inputs_hash ~ '^[0-9a-f]{64}$'),
            inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_generation_runs_account_time_idx
            ON quant.discipline_generation_runs (account_key, as_of_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_plans (
            plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid REFERENCES quant.discipline_generation_runs(run_id) ON DELETE SET NULL,
            plan_key text NOT NULL UNIQUE,
            contract_version text NOT NULL,
            account_key text NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            name text NOT NULL,
            plan_kind text NOT NULL CHECK (plan_kind IN ('holding', 'new_buy')),
            stage text NOT NULL,
            template_key text NOT NULL,
            template_version text NOT NULL,
            as_of_at timestamptz NOT NULL,
            trading_date date NOT NULL,
            valid_until timestamptz NOT NULL,
            position jsonb,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            sizing jsonb,
            lines jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            quality jsonb NOT NULL DEFAULT '[]'::jsonb,
            status text NOT NULL CHECK (status IN ('active', 'rejected_by_quality', 'superseded', 'expired')),
            supersedes_plan_id uuid REFERENCES quant.discipline_plans(plan_id) ON DELETE SET NULL,
            lowered_reason text,
            inputs_hash text NOT NULL,
            generator_version text NOT NULL,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_plans_account_symbol_time_idx
            ON quant.discipline_plans (account_key, symbol, as_of_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_plans_account_status_time_idx
            ON quant.discipline_plans (account_key, status, as_of_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_evaluations (
            evaluation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id uuid NOT NULL REFERENCES quant.discipline_plans(plan_id) ON DELETE CASCADE,
            as_of_at timestamptz NOT NULL,
            trading_date date NOT NULL,
            basis text NOT NULL CHECK (basis IN ('daily', 'minute')),
            line_states jsonb NOT NULL DEFAULT '[]'::jsonb,
            plan_state text NOT NULL CHECK (plan_state IN ('active', 'exit_signalled', 'reduce_signalled', 'expired')),
            inputs_hash text NOT NULL,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (plan_id, as_of_at, basis)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_evaluations_plan_time_idx
            ON quant.discipline_evaluations (plan_id, as_of_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_compliance (
            compliance_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id uuid NOT NULL REFERENCES quant.discipline_plans(plan_id) ON DELETE CASCADE,
            trade_record_id uuid,
            line_kind text,
            verdict text NOT NULL CHECK (verdict IN
                ('followed', 'early', 'late', 'missed', 'against_plan', 'unplanned')),
            deviation jsonb NOT NULL DEFAULT '{}'::jsonb,
            notes text NOT NULL DEFAULT '',
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (plan_id, content_hash)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_compliance_plan_time_idx
            ON quant.discipline_compliance (plan_id, created_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_reviews (
            review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id uuid NOT NULL REFERENCES quant.discipline_plans(plan_id) ON DELETE CASCADE,
            reviewer text NOT NULL,
            verdict text NOT NULL CHECK (verdict IN ('accept', 'override', 'reject')),
            notes text NOT NULL DEFAULT '',
            overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (plan_id, content_hash)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_reviews_plan_time_idx
            ON quant.discipline_reviews (plan_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.discipline_reviews")
    op.execute("DROP TABLE IF EXISTS quant.discipline_compliance")
    op.execute("DROP TABLE IF EXISTS quant.discipline_evaluations")
    op.execute("DROP TABLE IF EXISTS quant.discipline_plans")
    op.execute("DROP TABLE IF EXISTS quant.discipline_generation_runs")
