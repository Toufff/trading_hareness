"""Add durable runtime leases for cross-process orchestration.

Revision ID: 20260811_0002
Revises: 20260811_0001
"""

from alembic import op


revision = "20260811_0002"
down_revision = "20260811_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.runtime_leases (
            lease_key text PRIMARY KEY,
            holder_id uuid NOT NULL,
            acquired_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS runtime_leases_expires_idx
            ON quant.runtime_leases(expires_at)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.runtime_leases")
