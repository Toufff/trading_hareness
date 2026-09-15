"""Persist manual THS order-history exports.

Revision ID: 20260916_0099
Revises: 20260915_0098
"""
from alembic import op


revision = "20260916_0099"
down_revision = "20260915_0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_order_account_bindings (
            account_fingerprint text PRIMARY KEY CHECK (account_fingerprint ~ '^[0-9a-f]{64}$'),
            account_key text NOT NULL,
            broker text NOT NULL,
            masked_account text NOT NULL,
            confirmed_at timestamptz NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_order_imports (
            import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            source_path text NOT NULL,
            source_format text NOT NULL CHECK (source_format = 'ths_order_query_text_v1'),
            encoding text NOT NULL,
            row_count integer NOT NULL CHECK (row_count > 0),
            execution_count integer NOT NULL CHECK (execution_count >= 0 AND execution_count <= row_count),
            ignored_count integer NOT NULL CHECK (ignored_count >= 0),
            min_order_date date NOT NULL,
            max_order_date date NOT NULL CHECK (max_order_date >= min_order_date),
            imported_at timestamptz NOT NULL DEFAULT now(),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE(account_key, source_sha256)
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.broker_order_events (
            event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            account_key text NOT NULL,
            event_key text NOT NULL CHECK (event_key ~ '^[0-9a-f]{64}$'),
            first_import_id uuid NOT NULL REFERENCES quant.broker_order_imports(import_id) ON DELETE RESTRICT,
            source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
            order_date date NOT NULL,
            order_at timestamptz NOT NULL,
            symbol text REFERENCES quant.instruments(symbol) ON DELETE RESTRICT,
            raw_symbol text NOT NULL,
            name text NOT NULL,
            side text CHECK (side IN ('buy','sell')),
            raw_side text NOT NULL,
            status text NOT NULL,
            business_type text NOT NULL DEFAULT '',
            order_number text NOT NULL,
            market text NOT NULL DEFAULT '',
            order_kind text NOT NULL DEFAULT '',
            cancel_flag text NOT NULL DEFAULT '',
            order_quantity numeric NOT NULL CHECK (order_quantity >= 0),
            filled_quantity numeric NOT NULL CHECK (filled_quantity >= 0),
            gross_amount numeric NOT NULL CHECK (gross_amount >= 0),
            order_price numeric NOT NULL CHECK (order_price >= 0),
            fill_price numeric NOT NULL CHECK (fill_price >= 0),
            cancel_requested_quantity numeric NOT NULL CHECK (cancel_requested_quantity >= 0),
            cancelled_quantity numeric NOT NULL CHECK (cancelled_quantity >= 0),
            is_execution boolean NOT NULL,
            time_basis text NOT NULL CHECK (time_basis = 'order_time_proxy'),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(account_key,event_key)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS broker_order_events_timeline_idx
          ON quant.broker_order_events(account_key,symbol,order_at)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS broker_order_events_execution_idx
          ON quant.broker_order_events(account_key,order_date DESC,is_execution)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.broker_order_events")
    op.execute("DROP TABLE IF EXISTS quant.broker_order_imports")
    op.execute("DROP TABLE IF EXISTS quant.broker_order_account_bindings")
