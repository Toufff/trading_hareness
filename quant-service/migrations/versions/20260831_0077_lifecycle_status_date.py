"""Make lifecycle observations point-in-time and replay-safe."""

from alembic import op


revision = "20260831_0077"
down_revision = "20260831_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence ADD COLUMN IF NOT EXISTS status_date date")
    op.execute("UPDATE quant.instrument_lifecycle_evidence SET status_date=observed_at::date WHERE status_date IS NULL")
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence ALTER COLUMN status_date SET NOT NULL")
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence DROP CONSTRAINT IF EXISTS instrument_lifecycle_evidence_pkey")
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence ADD PRIMARY KEY(symbol,provider,status_date,list_status)")
    op.execute("CREATE INDEX IF NOT EXISTS instrument_lifecycle_status_date_idx ON quant.instrument_lifecycle_evidence(list_status,observed_at DESC,symbol)")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence DROP CONSTRAINT IF EXISTS instrument_lifecycle_evidence_pkey")
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence ADD PRIMARY KEY(symbol,provider,observed_at,list_status)")
    op.execute("ALTER TABLE quant.instrument_lifecycle_evidence DROP COLUMN IF EXISTS status_date")
