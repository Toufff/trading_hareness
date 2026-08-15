"""Add close-only sector-flow migration and LHB context.

Revision ID: 20260815_0033
Revises: 20260815_0032
"""

from alembic import op


revision = "20260815_0033"
down_revision = "20260815_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.sector_flow_daily_features (
            taxonomy_key text NOT NULL,
            sector_key text NOT NULL,
            trading_date date NOT NULL,
            provider_key text NOT NULL REFERENCES quant.providers(provider_key) ON DELETE RESTRICT,
            available_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('ready','partial','insufficient')),
            transition text NOT NULL,net_amount numeric,previous_net_amount numeric,
            net_change_amount numeric,net_acceleration numeric,rank_percentile numeric,
            flow_sign_streak integer NOT NULL DEFAULT 0,change_pct numeric,price_flow_divergence text,
            lhb_stock_count integer NOT NULL DEFAULT 0,lhb_net_amount numeric,
            lhb_negative_count integer NOT NULL DEFAULT 0,lhb_sell_pressure_ratio numeric,
            limit_up_count integer NOT NULL DEFAULT 0,features jsonb NOT NULL DEFAULT '{}'::jsonb,
            quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(taxonomy_key,sector_key,trading_date),
            FOREIGN KEY(taxonomy_key,sector_key) REFERENCES quant.sectors(taxonomy_key,sector_key) ON DELETE CASCADE
        )
    """)
    op.execute("""CREATE INDEX IF NOT EXISTS sector_flow_daily_feature_rank_idx
                     ON quant.sector_flow_daily_features(trading_date DESC,rank_percentile DESC)""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.sector_flow_daily_feature_rank_idx")
    op.execute("DROP TABLE IF EXISTS quant.sector_flow_daily_features")
