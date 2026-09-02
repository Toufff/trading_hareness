"""Add availability_basis to market_events so LEAST() merges stay honest.

Revision ID: 20260902_0088
Revises: 20260902_0087

The intraday/PIT audit found that ``persist_market_events``'s
``ON CONFLICT ... DO UPDATE SET available_at=LEAST(...)`` could take the
earlier of two ``available_at`` values that were derived completely
differently -- for example a genuine intraday capture timestamp merged
against a later post-close batch recompute's conservative 15:30 stamp, or
vice versa.  Blindly taking the minimum of two incomparable bases is not a
"more accurate" answer; it just picks whichever value happens to be earlier.

``availability_basis`` lets the application-layer merge (in
``app.public_market_repository.persist_market_events``) only apply
``LEAST()`` when the existing row and the incoming write share the same
basis, and otherwise leave the existing ``available_at`` untouched.  The
column is nullable text with no default; NULL means "unknown" (rows written
before this migration, or by a path that has not yet been updated to set it)
and the application treats NULL the same as any other single basis value for
comparison purposes.
"""

from alembic import op


revision = "20260902_0088"
down_revision = "20260902_0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.market_events ADD COLUMN IF NOT EXISTS availability_basis text")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.market_events DROP COLUMN IF EXISTS availability_basis")
