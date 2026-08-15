"""Add forward-only sector-flow outcome ledger.

Revision ID: 20260815_0034
Revises: 20260815_0033
"""

from alembic import op


revision = "20260815_0034"
down_revision = "20260815_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.sector_flow_daily_outcomes (
            taxonomy_key text NOT NULL,sector_key text NOT NULL,signal_date date NOT NULL,
            horizon_days integer NOT NULL CHECK (horizon_days IN (1,3,5)),transition text NOT NULL,
            status text NOT NULL CHECK (status IN ('pending','matured','unavailable')),
            entry_date date,exit_date date,entry_close numeric,exit_close numeric,raw_return numeric,
            cross_section_excess_return numeric,directional_return numeric,outcome_available_at timestamptz,
            quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(taxonomy_key,sector_key,signal_date,horizon_days),
            FOREIGN KEY(taxonomy_key,sector_key,signal_date)
                REFERENCES quant.sector_flow_daily_features(taxonomy_key,sector_key,trading_date) ON DELETE CASCADE
        )
    """)
    op.execute("""CREATE INDEX IF NOT EXISTS sector_flow_daily_outcome_status_idx
                     ON quant.sector_flow_daily_outcomes(status,horizon_days,signal_date DESC)""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.sector_flow_daily_outcome_status_idx")
    op.execute("DROP TABLE IF EXISTS quant.sector_flow_daily_outcomes")
