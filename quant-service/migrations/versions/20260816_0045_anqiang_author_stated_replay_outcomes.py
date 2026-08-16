"""Persist replay-only author-stated outcomes apart from live analyst facts.

Revision ID: 20260816_0045
Revises: 20260816_0044
"""

from alembic import op


revision = "20260816_0045"
down_revision = "20260816_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_action_intraday_outcomes (
            action_id uuid NOT NULL REFERENCES quant.analyst_trade_actions(action_id) ON DELETE CASCADE,
            methodology_version text NOT NULL,
            horizon_minutes integer NOT NULL CHECK (horizon_minutes IN (5,15,30,60)),
            status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
            entry_at timestamptz,
            entry_price numeric,
            exit_at timestamptz,
            exit_price numeric,
            directional_return numeric,
            source_name text,
            settlement jsonb NOT NULL DEFAULT '{}'::jsonb,
            calculated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(action_id,methodology_version,horizon_minutes)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS analyst_action_intraday_outcomes_status_idx
            ON quant.analyst_action_intraday_outcomes(methodology_version,status,calculated_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_action_intraday_outcomes")
