"""Record when a raw market observation row is modified after insertion.

Revision ID: 20260916_0101
Revises: 20260916_0100

The nightly backup exports ``raw_market_observations`` incrementally instead of
dumping the whole table.  The table is mostly append-only, but an identical
re-observation upserts ``available_at`` and the annual backfill rewrites
``available_at``/``ingested_at``, so a row exported on the day it was created
can change later.  A ``BEFORE UPDATE`` trigger stamps ``updated_at`` on every
modification, whichever code path (or manual repair) performs it; the backup
re-exports rows whose ``updated_at`` falls in its window and a restore applies
chunks in order, so the latest version wins.  Inserts are untouched and keep
``updated_at`` NULL, which also keeps the partial index tiny.

Every step checks the catalog first and only then takes a table lock, with a
short ``lock_timeout``: on a busy 10 GB table an unguarded ``ALTER TABLE``
queues behind a long read and blocks every writer queued behind it.  Apply it
by hand before a release when the table is busy; the start-up migration is
then a catalog-only no-op.
"""

from alembic import op


revision = "20260916_0101"
down_revision = "20260916_0100"
branch_labels = None
depends_on = None


UPDATED_AT_INDEX = "raw_market_updated_at_idx"
TRIGGER = "raw_market_observations_touch_updated_at"


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema='quant' AND table_name='raw_market_observations' AND column_name='updated_at'
            ) THEN
                SET LOCAL lock_timeout = '10s';
                ALTER TABLE quant.raw_market_observations ADD COLUMN updated_at timestamptz;
            END IF;
        END
        $$
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION quant.touch_raw_market_observation_updated_at()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
    """)
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger
                 WHERE tgname='{TRIGGER}' AND tgrelid='quant.raw_market_observations'::regclass
            ) THEN
                SET LOCAL lock_timeout = '10s';
                CREATE TRIGGER {TRIGGER}
                BEFORE UPDATE ON quant.raw_market_observations
                FOR EACH ROW EXECUTE FUNCTION quant.touch_raw_market_observation_updated_at();
            END IF;
        END
        $$
    """)
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {UPDATED_AT_INDEX} "
            "ON quant.raw_market_observations (updated_at) WHERE updated_at IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS quant.{UPDATED_AT_INDEX}")
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON quant.raw_market_observations")
    op.execute("DROP FUNCTION IF EXISTS quant.touch_raw_market_observation_updated_at()")
    op.execute("ALTER TABLE quant.raw_market_observations DROP COLUMN IF EXISTS updated_at")
