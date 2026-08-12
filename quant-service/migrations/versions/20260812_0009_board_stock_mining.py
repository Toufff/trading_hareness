"""Persist bounded, exact-membership intraday board stock-mining evidence.

Revision ID: 20260812_0009
Revises: 20260811_0008
"""

from alembic import op


revision = "20260812_0009"
down_revision = "20260811_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_board_stock_mining_runs (
            mining_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            board_report_id uuid NOT NULL UNIQUE REFERENCES quant.intraday_board_reports(board_report_id) ON DELETE CASCADE,
            observed_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('completed','partial','blocked','failed')),
            coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_board_stock_mining_candidates (
            candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            mining_run_id uuid NOT NULL REFERENCES quant.intraday_board_stock_mining_runs(mining_run_id) ON DELETE CASCADE,
            rank integer NOT NULL,
            direction text NOT NULL CHECK (direction IN ('inflow','outflow')),
            setup_key text NOT NULL,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            taxonomy_key text NOT NULL,
            sector_key text NOT NULL,
            label text NOT NULL,
            score numeric NOT NULL,
            board_net_inflow numeric,
            board_change_pct numeric,
            main_net_inflow numeric,
            volume_ratio numeric,
            turnover_rate numeric,
            pct_change numeric,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            UNIQUE(mining_run_id,direction,rank)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_board_stock_mining_runs_observed ON quant.intraday_board_stock_mining_runs(observed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_board_stock_mining_candidates_run_direction ON quant.intraday_board_stock_mining_candidates(mining_run_id,direction,rank)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_board_stock_mining_candidates")
    op.execute("DROP TABLE IF EXISTS quant.intraday_board_stock_mining_runs")
