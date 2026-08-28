"""Persist the daily short-term sentiment reading so strategies can stratify by it.

The stored regime classifies the four benchmark indices. Short-term A-share
practice reads a different tape: ladder height, how many boards broke, how
many of yesterday's held, and what yesterday's limit-ups actually paid. This
session's own 18,823-pair study showed those separate outcomes the index
regime does not - a board that opened three or more times returned +0.43%
next open-to-close against -0.11% for one that never opened.

It is a table of its own rather than more keys inside ``market_regime_daily``
because that row's ``model_version`` describes the index classifier; two
models sharing one version column cannot both be revised.

Revision ID: 20260828_0074
Revises: 20260828_0073
"""

from alembic import op


revision = "20260828_0074"
down_revision = "20260828_0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every count is NOT NULL because a session always has one; every rate is
    # nullable because a day with no attempted board has no broken rate and a
    # first session has no promotion rate, and recording 0.0 for either would
    # read as a frozen tape rather than an unknown one.
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.sentiment_cycle_daily (
            trading_date date PRIMARY KEY,
            model_version text NOT NULL,
            stage text NOT NULL,
            sealed_count integer NOT NULL,
            broken_count integer NOT NULL,
            broken_rate numeric,
            max_board_height integer NOT NULL,
            high_board_count integer NOT NULL,
            promotion_rate numeric,
            prior_limit_up_premium_pct numeric,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            calculated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS sentiment_cycle_daily_stage_idx
            ON quant.sentiment_cycle_daily(stage, trading_date)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.sentiment_cycle_daily")
