"""Atomic strategy changes and append-only independent review evidence."""
from alembic import op

revision = '20260911_0092'
down_revision = '20260911_0091'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_governance_items (
        id text PRIMARY KEY, dedupe_key text NOT NULL UNIQUE, revision integer NOT NULL,
        state text NOT NULL, snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_governance_events (
        item_id text NOT NULL REFERENCES quant.strategy_governance_items(id), revision integer NOT NULL,
        action text NOT NULL, actor_id text NOT NULL, evidence_hash text NOT NULL,
        snapshot jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(item_id,revision))''')
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_governance_activations (
        generation bigserial PRIMARY KEY, item_id text REFERENCES quant.strategy_governance_items(id),
        revision integer, artifact_hash text NOT NULL, actor_id text NOT NULL, action text NOT NULL,
        config jsonb NOT NULL, previous_config jsonb NOT NULL, code_hash text NOT NULL,
        previous_generation bigint NOT NULL, reason text NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_governance_leases (
        item_id text PRIMARY KEY REFERENCES quant.strategy_governance_items(id), revision integer NOT NULL,
        actor_id text NOT NULL, expires_at timestamptz NOT NULL)''')


def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.strategy_governance_leases')
    op.execute('DROP TABLE IF EXISTS quant.strategy_governance_activations')
    op.execute('DROP TABLE IF EXISTS quant.strategy_governance_events')
    op.execute('DROP TABLE IF EXISTS quant.strategy_governance_items')
