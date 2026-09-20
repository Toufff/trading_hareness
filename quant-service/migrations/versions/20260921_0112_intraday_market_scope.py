"""Allow replayable market-index and sector advisory events.

Revision ID: 20260921_0112
Revises: 20260921_0111
"""

from alembic import op


revision = "20260921_0112"
down_revision = "20260921_0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE quant.intraday_advisory_events
          DROP CONSTRAINT IF EXISTS intraday_advisory_events_scope_source_check;
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='intraday_advisory_events_scope_source_check' AND conrelid='quant.intraday_advisory_events'::regclass) THEN
            ALTER TABLE quant.intraday_advisory_events ADD CONSTRAINT intraday_advisory_events_scope_source_check
              CHECK(scope_source IN ('holding','recommendation','market_index','sector'));
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.intraday_advisory_events WHERE scope_source IN ('market_index','sector')")
    op.execute("""
        ALTER TABLE quant.intraday_advisory_events
          DROP CONSTRAINT IF EXISTS intraday_advisory_events_scope_source_check;
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='intraday_advisory_events_scope_source_check' AND conrelid='quant.intraday_advisory_events'::regclass) THEN
            ALTER TABLE quant.intraday_advisory_events ADD CONSTRAINT intraday_advisory_events_scope_source_check
              CHECK(scope_source IN ('holding','recommendation'));
          END IF;
        END $$
    """)
