"""Seed durable report cursors from already-imported immutable report versions.

Revision ID: 20260813_0019
Revises: 20260813_0018
"""

from alembic import op


revision = "20260813_0019"
down_revision = "20260813_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing reports were already imported before cursors existed.  Seed the
    # version map so enabling the delta workflow never re-fetches every detail
    # document and trips the remote rate limit on its first scheduled run.
    op.execute("""
        INSERT INTO quant.analyst_sync_cursors(
            stream_key, remote_analyst_id, report_versions, updated_at
        )
        SELECT
            'reports', remote_analyst_id,
            jsonb_object_agg(
                report_date::text,
                coalesce(remote_version, '') || ':' || coalesce(content_hash, '')
            ),
            now()
        FROM quant.remote_reports
        GROUP BY remote_analyst_id
        ON CONFLICT (stream_key, remote_analyst_id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM quant.analyst_sync_cursors WHERE stream_key='reports'")
