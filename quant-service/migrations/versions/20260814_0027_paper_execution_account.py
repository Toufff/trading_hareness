"""Add explicit paper account and immutable fill ledger.

Revision ID: 20260814_0027
Revises: 20260814_0026
"""

from alembic import op


revision = "20260814_0027"
down_revision = "20260814_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_accounts (
            account_key text PRIMARY KEY,
            initial_cash numeric NOT NULL CHECK (initial_cash >= 0),
            cash numeric NOT NULL CHECK (cash >= 0),
            configured_by text NOT NULL,
            configured_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.paper_order_fills (
            fill_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id uuid NOT NULL REFERENCES quant.paper_orders(order_id) ON DELETE CASCADE,
            decision_id uuid NOT NULL REFERENCES quant.paper_decisions(decision_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            side text NOT NULL CHECK (side IN ('buy','sell')),
            quantity integer NOT NULL CHECK (quantity > 0),
            price numeric NOT NULL CHECK (price > 0),
            fees numeric NOT NULL DEFAULT 0 CHECK (fees >= 0),
            slippage numeric NOT NULL DEFAULT 0 CHECK (slippage >= 0),
            filled_at timestamptz NOT NULL,
            source_name text NOT NULL,
            quote_observed_at timestamptz NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE(order_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS paper_order_fills_time_idx ON quant.paper_order_fills(filled_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.paper_order_fills")
    op.execute("DROP TABLE IF EXISTS quant.paper_accounts")
