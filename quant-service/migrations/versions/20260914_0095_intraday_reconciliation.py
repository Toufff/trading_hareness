"""Append-only source/close comparisons, never overwrite the source signal."""
from alembic import op
revision='20260914_0095'
down_revision='20260914_0094'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS quant.intraday_scan_reconciliations (
      source_run_id uuid NOT NULL REFERENCES quant.intraday_strategy_scans(run_id),
      close_run_id uuid NOT NULL REFERENCES quant.intraday_strategy_scans(run_id),
      result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY(source_run_id,close_run_id))''')

def downgrade():
    op.execute('DROP TABLE IF EXISTS quant.intraday_scan_reconciliations')
