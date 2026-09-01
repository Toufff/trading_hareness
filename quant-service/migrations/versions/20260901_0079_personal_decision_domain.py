"""Persist verified broker facts and terminal personal trade plans.

Revision ID: 20260901_0079
Revises: 20260901_0078
"""

from alembic import op


revision = "20260901_0079"
down_revision = "20260901_0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_portfolio_snapshots (
            snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            source text NOT NULL,
            source_snapshot_key text NOT NULL,
            observed_at timestamptz NOT NULL,
            verification text NOT NULL CHECK (verification IN ('verified_exact','verified_partial')),
            cash numeric CHECK (cash IS NULL OR cash >= 0),
            total_asset numeric CHECK (total_asset IS NULL OR total_asset >= 0),
            total_market_value numeric CHECK (total_market_value IS NULL OR total_market_value >= 0),
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(account_key,source,source_snapshot_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS broker_portfolio_snapshots_latest_idx
          ON quant.broker_portfolio_snapshots(account_key,observed_at DESC,recorded_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_position_snapshots (
            snapshot_id uuid NOT NULL REFERENCES quant.broker_portfolio_snapshots(snapshot_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            name text NOT NULL,
            quantity numeric NOT NULL CHECK (quantity >= 0),
            sellable_quantity numeric NOT NULL CHECK (sellable_quantity >= 0 AND sellable_quantity <= quantity),
            average_cost numeric CHECK (average_cost IS NULL OR average_cost >= 0),
            market_price numeric CHECK (market_price IS NULL OR market_price >= 0),
            market_value numeric CHECK (market_value IS NULL OR market_value >= 0),
            unrealized_pnl numeric,
            position_weight_pct numeric CHECK (position_weight_pct IS NULL OR position_weight_pct BETWEEN 0 AND 100),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY(snapshot_id,symbol)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS broker_position_snapshots_symbol_idx
          ON quant.broker_position_snapshots(symbol,snapshot_id)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.personal_trade_plans (
            plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_key text NOT NULL UNIQUE,
            plan_kind text NOT NULL CHECK (plan_kind IN ('holding','new_buy')),
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            name text NOT NULL,
            as_of_at timestamptz NOT NULL,
            valid_until timestamptz NOT NULL CHECK (valid_until > as_of_at),
            action text NOT NULL CHECK (action IN ('hold','observe','buy_on_trigger','reduce_on_trigger','exit_on_trigger','avoid')),
            entry_zone jsonb,
            add_trigger text,
            reduce_trigger text,
            exit_trigger text NOT NULL,
            stop_price numeric CHECK (stop_price IS NULL OR stop_price > 0),
            target_prices jsonb NOT NULL DEFAULT '[]'::jsonb,
            max_position_pct numeric NOT NULL CHECK (max_position_pct BETWEEN 0 AND 100),
            rationale jsonb NOT NULL,
            evidence_refs jsonb NOT NULL,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS personal_trade_plans_active_idx
          ON quant.personal_trade_plans(plan_kind,symbol,valid_until DESC,as_of_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.personal_trade_plans")
    op.execute("DROP TABLE IF EXISTS quant.broker_position_snapshots")
    op.execute("DROP TABLE IF EXISTS quant.broker_portfolio_snapshots")
