"""Store timestamped analyst trade-action research separately from claims.

Revision ID: 20260812_0013
Revises: 20260812_0012
"""

from alembic import op


revision = "20260812_0013"
down_revision = "20260812_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_trade_actions (
            action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            remote_report_id text NOT NULL REFERENCES quant.remote_reports(remote_report_id) ON DELETE CASCADE,
            remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id) ON DELETE CASCADE,
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            label text NOT NULL,
            action_type text NOT NULL CHECK (action_type IN ('buy','add_t','hold','trade','reduce','watch')),
            direction smallint NOT NULL CHECK (direction BETWEEN -1 AND 1),
            stated_at timestamptz NOT NULL,
            available_at timestamptz NOT NULL,
            target_price numeric,
            evidence text NOT NULL,
            raw jsonb NOT NULL DEFAULT '{}'::jsonb,
            content_sha256 text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(remote_report_id, content_sha256)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS analyst_trade_actions_time_idx ON quant.analyst_trade_actions(remote_analyst_id, stated_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS analyst_trade_actions_symbol_idx ON quant.analyst_trade_actions(symbol, stated_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_trade_actions")
