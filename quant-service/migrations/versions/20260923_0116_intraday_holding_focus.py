"""Explicit, expiring manual focus for held stocks; no order authority."""

from alembic import op

revision = '20260923_0116'
down_revision = '20260921_0115'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE quant.intraday_holding_focus (
      account_key text NOT NULL,
      symbol text NOT NULL,
      intent text NOT NULL CHECK (intent = 'intraday_t'),
      expires_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (account_key, symbol),
      CONSTRAINT intraday_holding_focus_symbol_check CHECK (symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$')
    );
    CREATE INDEX intraday_holding_focus_expiry_idx ON quant.intraday_holding_focus(expires_at);
    DO $$ BEGIN
      IF EXISTS(SELECT FROM pg_roles WHERE rolname='quant_app') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON quant.intraday_holding_focus TO quant_app;
      END IF;
    END $$;
    """)


def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.intraday_holding_focus')
