"""Daily cross-strategy watchlist proposals (read-only; never written into intraday_watchlists).

Revision ID: 20260825_0064
Revises: 20260825_0063
"""

from alembic import op


revision = "20260825_0064"
down_revision = "20260825_0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_watchlist_proposals (
            as_of_date date NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            proposal_rank integer NOT NULL,
            strategy_key text NOT NULL,
            raw_score numeric,
            score_scale text NOT NULL,
            strategy_percentile numeric NOT NULL,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(as_of_date, symbol)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS strategy_watchlist_proposals_date_idx
            ON quant.strategy_watchlist_proposals(as_of_date, proposal_rank)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.strategy_watchlist_proposals")
