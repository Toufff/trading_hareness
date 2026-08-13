"""Persist preregistered triple-barrier paper outcomes.

Revision ID: 20260814_0023
Revises: 20260814_0022
"""

from alembic import op


revision = "20260814_0023"
down_revision = "20260814_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_barrier_outcomes (
            signal_event_id uuid PRIMARY KEY REFERENCES quant.intraday_signal_events(signal_event_id) ON DELETE CASCADE,
            label_key text NOT NULL,
            upper_return numeric NOT NULL,
            lower_return numeric NOT NULL,
            max_horizon_minutes integer NOT NULL,
            entry_observed_at timestamptz NOT NULL,
            entry_price numeric NOT NULL,
            exit_observed_at timestamptz,
            exit_price numeric,
            label text CHECK (label IN ('upper','lower','time') OR label IS NULL),
            raw_return numeric,
            maximum_favorable_excursion numeric,
            maximum_adverse_excursion numeric,
            status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
            tradability text NOT NULL DEFAULT 'observed_quote_only',
            source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
            calculated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS paper_barrier_outcomes_status_idx ON quant.paper_barrier_outcomes(status,calculated_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.paper_barrier_outcomes")
