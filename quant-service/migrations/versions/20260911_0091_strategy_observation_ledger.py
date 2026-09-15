"""Freeze strategy discoveries independently of mutable latest-run projections."""
from alembic import op

revision='20260911_0091'
down_revision='20260910_0090'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_observation_origins (
        origin_id text PRIMARY KEY, signal_date date NOT NULL, symbol text NOT NULL,
        lane text NOT NULL, profile text NOT NULL, evidence jsonb NOT NULL,
        recorded_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE INDEX IF NOT EXISTS strategy_observation_date_idx ON quant.strategy_observation_origins(signal_date,symbol)')
    op.execute('''CREATE TABLE IF NOT EXISTS quant.strategy_observation_evaluations (
        origin_id text NOT NULL REFERENCES quant.strategy_observation_origins(origin_id),
        as_of_date date NOT NULL, model_version text NOT NULL, evidence_hash text NOT NULL,
        evidence jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(origin_id,as_of_date,model_version,evidence_hash))''')


def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.strategy_observation_evaluations')
    op.execute('DROP TABLE IF EXISTS quant.strategy_observation_origins')
