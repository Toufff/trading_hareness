"""Append-only human-attention decisions and frozen research input."""
from alembic import op
revision = '20260914_0096'
down_revision = '20260914_0095'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE quant.recommendation_pool_decisions (
        decision_id text PRIMARY KEY,
        run_id uuid NOT NULL REFERENCES quant.post_close_strategy_runs(run_id),
        as_of_date date NOT NULL, scan_hash text NOT NULL,
        context jsonb NOT NULL, review jsonb NOT NULL, result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now());
        CREATE INDEX recommendation_pool_run_idx ON quant.recommendation_pool_decisions(run_id,created_at DESC)''')


def downgrade():
    op.execute('DROP TABLE quant.recommendation_pool_decisions')
