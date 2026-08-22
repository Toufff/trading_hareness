"""Persist reproducible analyst x market daily and weekly reviews.

Revision ID: 20260822_0053
Revises: 20260817_0052
"""

from alembic import op

revision = "20260822_0053"
down_revision = "20260817_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.analyst_market_reviews (
            review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            cadence text NOT NULL CHECK (cadence IN ('daily','weekly')),
            period_start date NOT NULL,
            period_end date NOT NULL,
            status text NOT NULL CHECK (status IN ('ready','insufficient_history','partial','failed')),
            methodology_version text NOT NULL,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            generated_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(cadence, period_start, period_end)
        );
        CREATE INDEX IF NOT EXISTS analyst_market_reviews_latest_idx
            ON quant.analyst_market_reviews(cadence, period_end DESC, generated_at DESC);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.analyst_market_reviews")
