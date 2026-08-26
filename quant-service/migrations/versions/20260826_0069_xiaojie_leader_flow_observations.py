"""Intraday research observations for xiaojie-leader-flow-v1.

The strategy could previously only be called by hand with values a human
typed.  With the indicators now derived from the all-A cross-section every
scan, its verdicts need somewhere to land so they can be reviewed after the
fact and eventually replayed.

Revision ID: 20260826_0069
Revises: 20260826_0068
"""

from alembic import op


revision = "20260826_0069"
down_revision = "20260826_0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per (session, symbol, mode) rather than one per scan: a candidate
    # that holds its setup for an hour is one observation with a widening
    # window, not 120 duplicates.  first_seen_at is what makes "this is new"
    # answerable, which is what gates an alert.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.xiaojie_leader_flow_observations (
            trading_date date NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            mode text NOT NULL,
            model_version text NOT NULL,
            first_seen_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL,
            observation_count integer NOT NULL DEFAULT 1,
            first_scan_id uuid,
            decision text NOT NULL,
            target_fraction numeric,
            stop_loss jsonb NOT NULL DEFAULT '{}'::jsonb,
            exit_state jsonb NOT NULL DEFAULT '{}'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            market_gate jsonb NOT NULL DEFAULT '{}'::jsonb,
            first_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            alerted_at timestamptz,
            PRIMARY KEY(trading_date, symbol, mode)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS xiaojie_leader_flow_observations_seen_idx
            ON quant.xiaojie_leader_flow_observations(trading_date, first_seen_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.xiaojie_leader_flow_observations")
