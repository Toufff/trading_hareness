"""Durable transition/outbox state for trade-discipline notifications.

Revision ID: 20260920_0109
Revises: 20260920_0108

The alert lane is deliberately separate from ``intraday_signal_events``.  A
discipline line is a user/account-specific risk contract, not a market signal,
and pretending otherwise would corrupt both replay and delivery semantics.
"""

from alembic import op


revision = "20260920_0109"
down_revision = "20260920_0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_alert_line_states (
            plan_id uuid NOT NULL REFERENCES quant.discipline_plans(plan_id) ON DELETE CASCADE,
            line_key text NOT NULL,
            line_index integer NOT NULL CHECK (line_index >= 0),
            line_kind text NOT NULL,
            state text NOT NULL CHECK (state IN
                ('armed','triggered','expired','cancelled','capped')),
            evaluation_id uuid REFERENCES quant.discipline_evaluations(evaluation_id) ON DELETE SET NULL,
            baseline_suppressed boolean NOT NULL DEFAULT false,
            first_observed_at timestamptz NOT NULL,
            last_observed_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (plan_id, line_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_alert_line_states_updated_idx
            ON quant.discipline_alert_line_states (updated_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_alert_events (
            event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_key text NOT NULL UNIQUE,
            plan_id uuid NOT NULL REFERENCES quant.discipline_plans(plan_id) ON DELETE CASCADE,
            evaluation_id uuid NOT NULL REFERENCES quant.discipline_evaluations(evaluation_id) ON DELETE CASCADE,
            line_key text NOT NULL,
            line_index integer NOT NULL CHECK (line_index >= 0),
            line_kind text NOT NULL,
            from_state text NOT NULL CHECK (from_state='armed'),
            to_state text NOT NULL CHECK (to_state IN ('triggered','capped')),
            observed_at timestamptz NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_alert_events_plan_time_idx
            ON quant.discipline_alert_events (plan_id, observed_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_alert_deliveries (
            delivery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            event_id uuid NOT NULL REFERENCES quant.discipline_alert_events(event_id) ON DELETE CASCADE,
            channel text NOT NULL DEFAULT 'feishu' CHECK (channel='feishu'),
            status text NOT NULL CHECK (status IN ('pending','sent','failed','disabled')),
            message_text text NOT NULL,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz,
            response jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text,
            sent_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (event_id, channel)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS discipline_alert_deliveries_due_idx
            ON quant.discipline_alert_deliveries (next_attempt_at, created_at)
            WHERE status IN ('pending','failed')
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.discipline_alert_runtime_status (
            runtime_key text PRIMARY KEY CHECK (runtime_key='primary'),
            account_key text NOT NULL,
            state text NOT NULL CHECK (state IN
                ('disabled','idle','running','healthy','degraded','blocked','failed')),
            last_started_at timestamptz,
            last_completed_at timestamptz,
            last_session_date date,
            eligible_plans integer NOT NULL DEFAULT 0,
            evaluated_plans integer NOT NULL DEFAULT 0,
            emitted_events integer NOT NULL DEFAULT 0,
            pending_deliveries integer NOT NULL DEFAULT 0,
            last_reason text,
            last_error text,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.discipline_alert_runtime_status")
    op.execute("DROP TABLE IF EXISTS quant.discipline_alert_deliveries")
    op.execute("DROP TABLE IF EXISTS quant.discipline_alert_events")
    op.execute("DROP TABLE IF EXISTS quant.discipline_alert_line_states")
