"""Transactional factor-maintenance journal; trusted peer keeps full access."""
from alembic import op

revision = '20260921_0115'
down_revision = '20260921_0114'
branch_labels = None
depends_on = None
INDEXES = [('factor_maintenance_changes_tier_cutoff_idx',
            'quant.factor_maintenance_changes', 'recorded_at')]


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS quant.factor_maintenance_runs (
      run_id uuid PRIMARY KEY, operation text NOT NULL, actor text NOT NULL,
      database_role text NOT NULL DEFAULT session_user, source_version text NOT NULL,
      started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      finished_at timestamptz, status text NOT NULL DEFAULT 'running',
      change_count bigint NOT NULL DEFAULT 0,
      parameters jsonb NOT NULL DEFAULT '{}', result jsonb NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS factor_maintenance_runs_time_idx
      ON quant.factor_maintenance_runs(started_at DESC);
    CREATE TABLE IF NOT EXISTS quant.factor_maintenance_changes (
      change_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), change_no bigserial NOT NULL,
      run_id uuid NOT NULL, table_name text NOT NULL, row_key jsonb NOT NULL,
      before_image jsonb, after_image jsonb,
      recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX IF NOT EXISTS factor_maintenance_changes_run_idx
      ON quant.factor_maintenance_changes(run_id,change_no);
    CREATE INDEX IF NOT EXISTS factor_maintenance_changes_tier_cutoff_idx
      ON quant.factor_maintenance_changes(recorded_at);
    CREATE OR REPLACE FUNCTION quant.capture_factor_maintenance_change()
    RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE rid text; old_image jsonb; new_image jsonb; key_image jsonb;
    BEGIN
      rid := current_setting('quant.factor_run_id',true);
      IF rid IS NULL OR rid = '' THEN RETURN NULL; END IF;
      IF TG_OP <> 'INSERT' THEN old_image := to_jsonb(OLD); END IF;
      IF TG_OP <> 'DELETE' THEN new_image := to_jsonb(NEW); END IF;
      key_image := jsonb_build_object('symbol',coalesce(new_image,old_image)->'symbol',
                                     'trading_date',coalesce(new_image,old_image)->'trading_date');
      IF TG_TABLE_NAME = 'daily_adjustment_factors' THEN
        key_image := key_image || jsonb_build_object('provider',coalesce(new_image,old_image)->'provider');
      ELSE
        IF old_image IS NOT NULL THEN old_image := jsonb_build_object('adj_factor',old_image->'adj_factor'); END IF;
        IF new_image IS NOT NULL THEN new_image := jsonb_build_object('adj_factor',new_image->'adj_factor'); END IF;
      END IF;
      IF old_image IS NOT DISTINCT FROM new_image THEN RETURN NULL; END IF;
      INSERT INTO quant.factor_maintenance_changes(run_id,table_name,row_key,before_image,after_image)
      VALUES(rid::uuid,TG_TABLE_NAME,key_image,old_image,new_image);
      RETURN NULL;
    END $$;
    """)
    for table in ('daily_adjustment_factors', 'canonical_bars_daily', 'market_bars_daily'):
        op.execute(f'DROP TRIGGER IF EXISTS factor_maintenance_capture ON quant.{table}')
        op.execute(f'''CREATE TRIGGER factor_maintenance_capture AFTER INSERT OR UPDATE OR DELETE
                      ON quant.{table} FOR EACH ROW EXECUTE FUNCTION quant.capture_factor_maintenance_change()''')
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT FROM pg_roles WHERE rolname='stock_peer') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON quant.factor_maintenance_runs,
          quant.factor_maintenance_changes TO stock_peer;
        GRANT USAGE,SELECT ON SEQUENCE quant.factor_maintenance_changes_change_no_seq TO stock_peer;
      END IF;
      IF EXISTS(SELECT FROM pg_roles WHERE rolname='quant_app') THEN
        GRANT SELECT,INSERT,UPDATE,DELETE ON quant.factor_maintenance_runs,
          quant.factor_maintenance_changes TO quant_app;
        GRANT USAGE,SELECT ON SEQUENCE quant.factor_maintenance_changes_change_no_seq TO quant_app;
      END IF;
    END $$;""")


def downgrade():
    # Deliberately retain recovery evidence on code rollback.
    for table in ('daily_adjustment_factors', 'canonical_bars_daily', 'market_bars_daily'):
        op.execute(f'DROP TRIGGER IF EXISTS factor_maintenance_capture ON quant.{table}')
