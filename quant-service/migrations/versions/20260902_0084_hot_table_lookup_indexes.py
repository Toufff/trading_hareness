"""Add lookup indexes for the hottest intraday and raw-record tables.

Revision ID: 20260902_0084
Revises: 20260901_0083

Every index is created ``CONCURRENTLY`` inside an ``autocommit_block`` (the
0050 pattern) so a live collector is never blocked by a table-wide lock.
"""

from alembic import op


revision = "20260902_0084"
down_revision = "20260901_0083"
branch_labels = None
depends_on = None


# (index name, table, definition) -- kept as data so tests can pin the set.
INDEXES = (
    # post-close normalization looks rows up by ``row_data->>'ts_code'``; without
    # this expression index every lookup is a sequential scan over raw records.
    ("tushare_raw_api_ts_code_idx", "quant.tushare_raw_records",
     "(api_name, ((row_data->>'ts_code')))"),
    # intraday monitor: three lookups per tick per watch entry.
    ("intraday_signal_events_symbol_observed_idx", "quant.intraday_signal_events",
     "(symbol, observed_at DESC)"),
    ("intraday_signal_events_observed_idx", "quant.intraday_signal_events",
     "(observed_at DESC)"),
    ("intraday_signal_events_scan_idx", "quant.intraday_signal_events",
     "(scan_id)"),
    ("data_quality_issues_symbol_date_idx", "quant.data_quality_issues",
     "(symbol, trading_date DESC)"),
    ("intraday_signal_episodes_symbol_session_idx", "quant.intraday_signal_episodes",
     "(symbol, session_date DESC)"),
    # Minute bars are appended in time order, so a BRIN index makes range
    # deletes/reads by bar_time cheap without a large B-tree.
    ("market_bars_minute_bar_time_brin_idx", "quant.market_bars_minute",
     "USING brin (bar_time)"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, definition in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS quant.{name}")
