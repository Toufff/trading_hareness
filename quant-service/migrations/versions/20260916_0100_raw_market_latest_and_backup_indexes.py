"""Index the all-A latest-snapshot lookup and the incremental backup window.

Revision ID: 20260916_0100
Revises: 20260916_0099

``SELECT max(effective_at) ... WHERE capability='a_share_prices_snapshot'``
could only use ``raw_market_lookup_idx (capability, symbol, effective_at)``,
so every call walked every snapshot row of that capability.  On the HDD that
holds the platform database this timed out more than a thousand times a day
and starved the nightly ``pg_dump``.  ``(capability, effective_at DESC)``
turns it into a single index probe.

The nightly backup exports ``raw_market_observations`` incrementally by
``created_at``; the table is append-only in insertion order, so a BRIN index
keeps each window read to the matching block ranges without a large B-tree.

Both indexes are created ``CONCURRENTLY`` inside an ``autocommit_block`` (the
0084 pattern).  On a large production table build them by hand before a
release with the same statements; ``IF NOT EXISTS`` then makes this a no-op
instead of holding up service start-up.
"""

from alembic import op


revision = "20260916_0100"
down_revision = "20260916_0099"
branch_labels = None
depends_on = None


# (index name, table, definition) -- kept as data so tests can pin the set.
INDEXES = (
    ("raw_market_capability_effective_idx", "quant.raw_market_observations",
     "(capability, effective_at DESC)"),
    ("raw_market_created_at_brin_idx", "quant.raw_market_observations",
     "USING brin (created_at)"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, definition in INDEXES:
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} {definition}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, _table, _definition in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS quant.{name}")
