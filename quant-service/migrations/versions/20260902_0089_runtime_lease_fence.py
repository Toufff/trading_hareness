"""Add a monotonic fencing token to quant.runtime_leases.

Revision ID: 20260902_0089
Revises: 20260902_0088

Audit (section B, HIGH): a lease loses effect the moment its holder's renew
fails, but any write already dispatched to the bounded blocking executor
keeps running to completion regardless (a started thread cannot be
cancelled). ``fence`` is a classic fencing token: it increments only when a
new ownership epoch begins (a genuine ``acquire`` of an absent/expired
lease), stays stable across every renewal by the same holder, and a write
path captures it once at acquire time and can cheaply compare it against the
live column before committing to detect a stale holder.

This can be applied online: the column is additive with a default, existing
rows backfill to 0 (below the first real ``acquire``'s value of 1), and no
code path reads it until this same deploy's ``runtime_leases.py``/
``async_runtime_lease_repository.py`` start writing it.
"""

from alembic import op


revision = "20260902_0089"
down_revision = "20260902_0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.runtime_leases ADD COLUMN IF NOT EXISTS fence bigint NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.runtime_leases DROP COLUMN IF EXISTS fence")
