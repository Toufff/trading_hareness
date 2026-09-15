"""Immutable-input intraday evidence runs, distinct from close results."""
from alembic import op
revision='20260914_0094'
down_revision='20260911_0093'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS quant.intraday_strategy_scans (
        run_id uuid PRIMARY KEY,cutoff timestamptz NOT NULL,model_version text NOT NULL,
        state text NOT NULL CHECK(state IN ('running','completed','failed')),stage text NOT NULL,
        input_hash text,input jsonb,result jsonb,error text,
        created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE INDEX IF NOT EXISTS intraday_strategy_scans_time_idx ON quant.intraday_strategy_scans(cutoff DESC)')

def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.intraday_strategy_scans')
