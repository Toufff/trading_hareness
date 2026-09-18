"""Index the storage tier cutoff column of every tiered evidence table.

Revision ID: 20260919_0106
Revises: 20260918_0105

``scripts/database-storage-tiers.py`` drains each tiered table with
``SELECT ... WHERE <cutoff column> < %(cutoff)s ORDER BY <cutoff column>
LIMIT <batch>``.  Three of the five tables have no index leading with that
column, so every batch re-scanned and re-sorted a whole index: measured on the
live cluster, ``quant.raw_market_observations`` (19 GB, 7.7 M rows) planned as
``Limit -> Sort -> Index Scan using raw_market_availability_basis_idx``, i.e. a
full pass per 20 000 rows.  With the index each batch is a bounded range scan.

``install`` creates exactly these five index names on its autocommit
connection, so a live cluster gets them without waiting for a release; this
migration is what gives them to a database rebuilt from the chain (and to the
acceptance database the release gate rebuilds).  ``IF NOT EXISTS`` makes
whichever runs second a no-op.

Both directions run ``CONCURRENTLY`` inside an ``op.get_context()
.autocommit_block()`` -- the 0084/0100 pattern -- because these tables are
written to continuously and an ``ACCESS EXCLUSIVE`` index build would stall the
ingestion path.

**Repair is ``install``'s job, not this migration's.**  A ``CREATE INDEX
CONCURRENTLY`` that is cancelled (lock timeout, statement timeout, a killed
backend) leaves an index with ``indisvalid = false`` holding the name.  The
planner ignores it and ``IF NOT EXISTS`` matches it, so this migration is a
no-op against a database in that state -- deliberately, because a migration
must not spend two hours rebuilding a 19 GB index while a release is gated on
it.  ``_install_cutoff_index`` in ``scripts/database-storage-tiers.py`` looks
the name up in ``pg_index``, drops an invalid one with ``DROP INDEX
CONCURRENTLY`` and rebuilds it (``rebuilt_invalid``), or reports
``invalid_index_present`` and finishes ``partial`` when it cannot take the
lock.  ``plan``/``status`` surface the same condition as
``cutoff_index_valid = false``.
"""

from alembic import op


revision = "20260919_0106"
down_revision = "20260918_0105"
branch_labels = None
depends_on = None


# (index name, table, cutoff column) -- kept as data so the tests can pin the
# set against TIER_POLICY in scripts/database-storage-tiers.py.
INDEXES = (
    ("raw_market_observations_tier_cutoff_idx", "quant.raw_market_observations", "available_at"),
    ("tushare_raw_records_tier_cutoff_idx", "quant.tushare_raw_records", "available_at"),
    ("intraday_quote_observations_tier_cutoff_idx", "quant.intraday_quote_observations", "observed_at"),
    ("intraday_rule_input_snapshots_tier_cutoff_idx", "quant.intraday_rule_input_snapshots", "observed_at"),
    ("edge_evidence_changes_tier_cutoff_idx", "quant.edge_evidence_changes", "changed_at"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({column})")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _column in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS quant.{name}")
