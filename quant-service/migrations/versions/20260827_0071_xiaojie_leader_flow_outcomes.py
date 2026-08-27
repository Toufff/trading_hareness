"""Settled outcomes for xiaojie leader-flow observations.

Observations record what was true when a name was flagged; nothing recorded
what happened afterwards, so a week of accumulation would still not answer
whether any mode works. This is the counterpart table, built the same way
strategy_daily_candidate_outcomes settles the ledger.

Revision ID: 20260827_0071
Revises: 20260826_0070
"""

from alembic import op


revision = "20260827_0071"
down_revision = "20260826_0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keyed identically to the observation it settles.  ``sealed_at_entry`` is
    # a column rather than buried in evidence because it is the split that
    # decides whether a row is evaluable at all: on 2026-08-27 the 63
    # observations already locked at the limit when flagged produced zero gains,
    # so mixing them into an average silently halves every mode's measured edge.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.xiaojie_leader_flow_outcomes (
            trading_date date NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            mode text NOT NULL,
            model_version text NOT NULL,
            first_seen_at timestamptz NOT NULL,
            alerted boolean NOT NULL DEFAULT false,
            sealed_at_entry boolean NOT NULL DEFAULT false,
            entry_price numeric NOT NULL,
            session_close numeric,
            session_return_pct numeric,
            next_open numeric,
            next_close numeric,
            next_open_locked boolean,
            entry_to_next_close_pct numeric,
            next_open_to_close_pct numeric,
            benchmark_session_pct numeric,
            excess_session_pct numeric,
            settled_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(trading_date, symbol, mode)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS xiaojie_leader_flow_outcomes_mode_idx
            ON quant.xiaojie_leader_flow_outcomes(mode, trading_date)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.xiaojie_leader_flow_outcomes")
