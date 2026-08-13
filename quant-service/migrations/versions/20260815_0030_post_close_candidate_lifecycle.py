"""Add lifecycle metadata to bounded post-close research candidates.

Revision ID: 20260815_0030
Revises: 20260814_0029
"""

from alembic import op


revision = "20260815_0030"
down_revision = "20260814_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quant.post_close_strategy_candidates ADD COLUMN IF NOT EXISTS discovered_at timestamptz NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates ADD COLUMN IF NOT EXISTS expires_at timestamptz")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates ADD COLUMN IF NOT EXISTS reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates ADD COLUMN IF NOT EXISTS source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("CREATE INDEX IF NOT EXISTS post_close_candidates_expiry_idx ON quant.post_close_strategy_candidates(expires_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.post_close_candidates_expiry_idx")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates DROP COLUMN IF EXISTS source_snapshot")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates DROP COLUMN IF EXISTS reason_codes")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates DROP COLUMN IF EXISTS expires_at")
    op.execute("ALTER TABLE quant.post_close_strategy_candidates DROP COLUMN IF EXISTS discovered_at")
