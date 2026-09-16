"""Agent-operated paper accounts compared against the human broker account.

Revision ID: 20260917_0102
Revises: 20260916_0101

An LLM agent trades its own simulated account from the same point-in-time
information the human sees.  The legacy ``paper_*`` tables are keyed by symbol
alone and hardwired to one shared ledger, so the agent ledger is separate and
keyed by ``account_key`` throughout.  Nothing here is an order path: every row
is paper-only research evidence.
"""

from alembic import op


revision = "20260917_0102"
down_revision = "20260916_0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.agent_paper_accounts (
            account_key text PRIMARY KEY,
            model text NOT NULL,
            start_date date NOT NULL,
            initial_equity numeric NOT NULL,
            cash numeric NOT NULL,
            baseline jsonb NOT NULL DEFAULT '{}'::jsonb,
            memory jsonb NOT NULL DEFAULT '{}'::jsonb,
            last_roll_date date,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.agent_paper_positions (
            account_key text NOT NULL REFERENCES quant.agent_paper_accounts(account_key) ON DELETE CASCADE,
            symbol text NOT NULL,
            name text,
            quantity integer NOT NULL,
            sellable_quantity integer NOT NULL,
            average_cost numeric NOT NULL,
            realized_pnl numeric NOT NULL DEFAULT 0,
            last_buy_date date,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (account_key, symbol)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.agent_paper_decisions (
            decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL REFERENCES quant.agent_paper_accounts(account_key) ON DELETE CASCADE,
            decided_at timestamptz NOT NULL,
            trading_date date NOT NULL,
            status text NOT NULL,
            model text NOT NULL,
            context_hash text,
            context_chars integer,
            output jsonb,
            error text,
            usage jsonb NOT NULL DEFAULT '{}'::jsonb,
            duration_ms integer,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS agent_paper_decisions_account_time_idx
            ON quant.agent_paper_decisions (account_key, decided_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.agent_paper_orders (
            order_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL REFERENCES quant.agent_paper_accounts(account_key) ON DELETE CASCADE,
            decision_id uuid REFERENCES quant.agent_paper_decisions(decision_id) ON DELETE SET NULL,
            trading_date date NOT NULL,
            symbol text NOT NULL,
            name text,
            side text NOT NULL CHECK (side IN ('buy', 'sell')),
            order_type text NOT NULL CHECK (order_type IN ('market', 'limit')),
            quantity integer NOT NULL,
            limit_price numeric,
            status text NOT NULL,
            filled_quantity integer NOT NULL DEFAULT 0,
            fill_price numeric,
            fees numeric NOT NULL DEFAULT 0,
            reason text,
            reject_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            quote jsonb NOT NULL DEFAULT '{}'::jsonb,
            placed_at timestamptz NOT NULL,
            filled_at timestamptz,
            closed_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS agent_paper_orders_account_day_idx
            ON quant.agent_paper_orders (account_key, trading_date, placed_at)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.agent_paper_nav (
            account_key text NOT NULL REFERENCES quant.agent_paper_accounts(account_key) ON DELETE CASCADE,
            as_of timestamptz NOT NULL,
            trading_date date NOT NULL,
            cash numeric NOT NULL,
            market_value numeric NOT NULL,
            equity numeric NOT NULL,
            price_basis text NOT NULL,
            positions jsonb NOT NULL DEFAULT '[]'::jsonb,
            PRIMARY KEY (account_key, as_of)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.agent_paper_nav")
    op.execute("DROP TABLE IF EXISTS quant.agent_paper_orders")
    op.execute("DROP TABLE IF EXISTS quant.agent_paper_decisions")
    op.execute("DROP TABLE IF EXISTS quant.agent_paper_positions")
    op.execute("DROP TABLE IF EXISTS quant.agent_paper_accounts")
