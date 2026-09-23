"""Allow a recommendation decision to bind to either settled or noon scan."""

from alembic import op

revision = '20260923_0117'
down_revision = '20260923_0116'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''ALTER TABLE quant.recommendation_pool_decisions
        ALTER COLUMN run_id DROP NOT NULL;
        ALTER TABLE quant.recommendation_pool_decisions
        ADD COLUMN IF NOT EXISTS intraday_run_id uuid REFERENCES quant.intraday_strategy_scans(run_id);
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='recommendation_exactly_one_scan' AND conrelid='quant.recommendation_pool_decisions'::regclass) THEN
            ALTER TABLE quant.recommendation_pool_decisions
              ADD CONSTRAINT recommendation_exactly_one_scan CHECK
                ((run_id IS NOT NULL AND intraday_run_id IS NULL) OR
                 (run_id IS NULL AND intraday_run_id IS NOT NULL));
          END IF;
        END $$;
        CREATE INDEX IF NOT EXISTS recommendation_pool_intraday_run_idx
          ON quant.recommendation_pool_decisions(intraday_run_id,created_at DESC)''')


def downgrade():
    op.execute('''DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM quant.recommendation_pool_decisions
                     WHERE intraday_run_id IS NOT NULL) THEN
            RAISE EXCEPTION 'noon recommendation history must be archived before downgrade';
          END IF;
        END $$;
        DROP INDEX IF EXISTS quant.recommendation_pool_intraday_run_idx;
        ALTER TABLE quant.recommendation_pool_decisions
          DROP CONSTRAINT IF EXISTS recommendation_exactly_one_scan;
        ALTER TABLE quant.recommendation_pool_decisions
          DROP COLUMN IF EXISTS intraday_run_id;
        ALTER TABLE quant.recommendation_pool_decisions
          ALTER COLUMN run_id SET NOT NULL''')
