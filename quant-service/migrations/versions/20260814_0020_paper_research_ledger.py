"""Versioned paper-trading research ledger; never submits broker orders.

Revision ID: 20260814_0020
Revises: 20260813_0019
"""

from alembic import op


revision = "20260814_0020"
down_revision = "20260813_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_trials (
            trial_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            strategy_key text NOT NULL,
            strategy_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('draft','shadow','paper','champion','challenger','disabled','rejected')),
            hypothesis text NOT NULL DEFAULT '',
            data_boundary jsonb NOT NULL DEFAULT '{}'::jsonb,
            parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
            approved_by text,
            approved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(strategy_key, strategy_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_contracts (
            strategy_key text NOT NULL,
            strategy_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('research_only','shadow','paper','champion','challenger','disabled')),
            contract jsonb NOT NULL DEFAULT '{}'::jsonb,
            trial_id uuid REFERENCES quant.strategy_trials(trial_id),
            approved_by text,
            approved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(strategy_key, strategy_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_decisions (
            decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            signal_event_id uuid NOT NULL REFERENCES quant.intraday_signal_events(signal_event_id) ON DELETE CASCADE,
            strategy_key text NOT NULL,
            strategy_version text NOT NULL,
            trial_id uuid REFERENCES quant.strategy_trials(trial_id),
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
            status text NOT NULL CHECK (status IN ('proposed','blocked','accepted','expired','cancelled')),
            decision_at timestamptz NOT NULL,
            target_quantity integer NOT NULL DEFAULT 0 CHECK (target_quantity >= 0),
            target_weight numeric NOT NULL DEFAULT 0 CHECK (target_weight >= 0 AND target_weight <= 1),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(signal_event_id, strategy_key, strategy_version)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_orders (
            order_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            decision_id uuid NOT NULL REFERENCES quant.paper_decisions(decision_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            side text NOT NULL CHECK (side IN ('buy','sell')),
            requested_quantity integer NOT NULL CHECK (requested_quantity >= 0),
            accepted_quantity integer NOT NULL DEFAULT 0 CHECK (accepted_quantity >= 0),
            filled_quantity integer NOT NULL DEFAULT 0 CHECK (filled_quantity >= 0),
            limit_price numeric,
            average_fill_price numeric,
            status text NOT NULL CHECK (status IN ('proposed','accepted','partially_filled','filled','non_fill','cancelled')),
            fees numeric NOT NULL DEFAULT 0,
            slippage numeric NOT NULL DEFAULT 0,
            submitted_at timestamptz NOT NULL,
            filled_at timestamptz,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_positions (
            symbol text PRIMARY KEY REFERENCES quant.instruments(symbol),
            quantity integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
            sellable_quantity integer NOT NULL DEFAULT 0 CHECK (sellable_quantity >= 0),
            average_cost numeric NOT NULL DEFAULT 0,
            buy_date date,
            realized_pnl numeric NOT NULL DEFAULT 0,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_portfolio_snapshots (
            snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            as_of timestamptz NOT NULL UNIQUE,
            cash numeric NOT NULL DEFAULT 0,
            equity numeric NOT NULL DEFAULT 0,
            gross_exposure numeric NOT NULL DEFAULT 0,
            net_exposure numeric NOT NULL DEFAULT 0,
            drawdown numeric NOT NULL DEFAULT 0,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_risk_events (
            risk_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            decision_id uuid REFERENCES quant.paper_decisions(decision_id) ON DELETE SET NULL,
            symbol text,
            event_type text NOT NULL,
            severity text NOT NULL CHECK (severity IN ('info','warning','block','critical')),
            message text NOT NULL,
            occurred_at timestamptz NOT NULL,
            details jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS paper_decisions_time_idx ON quant.paper_decisions(decision_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS paper_orders_time_idx ON quant.paper_orders(submitted_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS paper_risk_events_time_idx ON quant.paper_risk_events(occurred_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.paper_risk_events")
    op.execute("DROP TABLE IF EXISTS quant.paper_portfolio_snapshots")
    op.execute("DROP TABLE IF EXISTS quant.paper_positions")
    op.execute("DROP TABLE IF EXISTS quant.paper_orders")
    op.execute("DROP TABLE IF EXISTS quant.paper_decisions")
    op.execute("DROP TABLE IF EXISTS quant.strategy_contracts")
    op.execute("DROP TABLE IF EXISTS quant.strategy_trials")
