"""Add immutable broker trade records parsed from client exports.

Revision ID: 20260915_0098
Revises: 20260914_0097
"""
from alembic import op


revision = "20260915_0098"
down_revision = "20260914_0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_trade_records (
            record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            trade_key text NOT NULL CHECK (trade_key ~ '^[0-9a-f]{64}$'),
            broker text NOT NULL,
            trade_date date NOT NULL,
            trade_time time,
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            name text NOT NULL,
            side text NOT NULL CHECK (side IN ('buy','sell')),
            quantity numeric NOT NULL CHECK (quantity > 0),
            price numeric CHECK (price IS NULL OR price >= 0),
            gross_amount numeric CHECK (gross_amount IS NULL OR gross_amount >= 0),
            net_amount numeric,
            commission numeric NOT NULL DEFAULT 0 CHECK (commission >= 0),
            stamp_duty numeric NOT NULL DEFAULT 0 CHECK (stamp_duty >= 0),
            transfer_fee numeric NOT NULL DEFAULT 0 CHECK (transfer_fee >= 0),
            other_fee numeric NOT NULL DEFAULT 0 CHECK (other_fee >= 0),
            currency text NOT NULL DEFAULT '人民币',
            source text NOT NULL,
            source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            observed_at timestamptz NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(account_key,trade_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS broker_trade_records_review_idx
          ON quant.broker_trade_records(account_key,trade_date DESC,trade_time DESC,symbol)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.broker_trade_records")
