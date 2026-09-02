"""Configurable row retention for the unbounded minute/quote/raw tables.

Revision ID: 20260902_0085
Revises: 20260902_0084

``quant.retention_policies`` holds one row per table (time column, days,
batch size); ``quant.apply_retention_policy(table)`` deletes one bounded batch
of rows older than ``now() - retention_days`` and returns the count.  Nothing
is scheduled here and every seeded policy starts ``enabled=false``: an
operator (or a later task-registry job) enables a policy explicitly and runs
``quant-service/retention_maintenance.py`` which loops batches with a commit
between each.  No partitioning is introduced.
"""

from alembic import op


revision = "20260902_0085"
down_revision = "20260902_0084"
branch_labels = None
depends_on = None


# table_name, time_column, retention_days, batch_size.  ``tushare_raw_records``
# keys on ``created_at`` (ingest time) rather than ``available_at`` so a
# historical backfill is never deleted the moment it lands.
SEED_POLICIES = (
    ("market_bars_minute", "bar_time", 400, 20000),
    ("intraday_minute_sessions", "bar_time", 200, 20000),
    ("intraday_quote_observations", "observed_at", 45, 20000),
    ("tushare_raw_records", "created_at", 400, 20000),
)

RETENTION_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION quant.apply_retention_policy(p_table_name text, p_batch_size integer DEFAULT NULL)
RETURNS TABLE(deleted_rows bigint, cutoff timestamptz)
LANGUAGE plpgsql AS $$
DECLARE
    policy quant.retention_policies%ROWTYPE;
    batch integer;
    removed bigint := 0;
BEGIN
    SELECT * INTO policy FROM quant.retention_policies p WHERE p.table_name = p_table_name;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'no retention policy for quant.%', p_table_name;
    END IF;
    IF NOT policy.enabled THEN
        RAISE EXCEPTION 'retention policy for quant.% is disabled', p_table_name;
    END IF;
    batch := coalesce(p_batch_size, policy.batch_size);
    IF batch <= 0 THEN
        RAISE EXCEPTION 'batch size must be positive';
    END IF;
    cutoff := now() - make_interval(days => policy.retention_days);
    EXECUTE format(
        'DELETE FROM quant.%I t WHERE t.ctid = ANY(ARRAY(SELECT s.ctid FROM quant.%I s WHERE s.%I < $1 LIMIT $2))',
        policy.table_name, policy.table_name, policy.time_column)
    USING cutoff, batch;
    GET DIAGNOSTICS removed = ROW_COUNT;
    UPDATE quant.retention_policies p
       SET last_run_at = now(),
           last_deleted_rows = coalesce(p.last_deleted_rows, 0) + removed,
           updated_at = now()
     WHERE p.table_name = p_table_name;
    deleted_rows := removed;
    RETURN NEXT;
END
$$
"""


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.retention_policies (
            table_name text PRIMARY KEY CHECK (table_name ~ '^[a-z_][a-z0-9_]*$'),
            time_column text NOT NULL CHECK (time_column ~ '^[a-z_][a-z0-9_]*$'),
            retention_days integer NOT NULL CHECK (retention_days > 0),
            batch_size integer NOT NULL DEFAULT 20000 CHECK (batch_size > 0),
            enabled boolean NOT NULL DEFAULT false,
            last_run_at timestamptz,
            last_deleted_rows bigint,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    for table_name, time_column, retention_days, batch_size in SEED_POLICIES:
        op.execute(f"""
            INSERT INTO quant.retention_policies(table_name, time_column, retention_days, batch_size, enabled)
            VALUES ('{table_name}', '{time_column}', {retention_days}, {batch_size}, false)
            ON CONFLICT (table_name) DO NOTHING
        """)
    op.execute(RETENTION_FUNCTION_SQL)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS quant.apply_retention_policy(text, integer)")
    op.execute("DROP TABLE IF EXISTS quant.retention_policies")
