"""Durable deduplicated governance waits, without pretending strategy progress."""
from alembic import op
revision='20260911_0093'
down_revision='20260911_0092'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_governance_diagnostics (
        item_id text NOT NULL REFERENCES quant.strategy_governance_items(id), fingerprint text NOT NULL,
        evidence jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(item_id,fingerprint))''')
    op.execute('CREATE INDEX IF NOT EXISTS strategy_governance_diagnostic_time_idx ON quant.strategy_governance_diagnostics(item_id,recorded_at DESC)')


def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.strategy_governance_diagnostics')
