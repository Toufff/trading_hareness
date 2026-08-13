"""Archive remote analyst messages with immutable strategy availability.

Revision ID: 20260813_0017
Revises: 20260812_0016
"""

from alembic import op


revision = "20260813_0017"
down_revision = "20260812_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.remote_analyst_messages (
            remote_message_id text PRIMARY KEY,
            remote_analyst_id text NOT NULL REFERENCES quant.remote_analysts(remote_analyst_id),
            source_item_id text NOT NULL, source_message_id text, source_entry_id text,
            source_type text NOT NULL CHECK (source_type IN ('text','url','image_ocr','audio','video')),
            source_ref text NOT NULL DEFAULT '', content text NOT NULL, content_hash text NOT NULL,
            remote_version text NOT NULL, received_at timestamptz NOT NULL,
            strategy_available_at timestamptz NOT NULL, source_published_at timestamptz,
            source_edited_at timestamptz, stated_at timestamptz,
            stated_precision text CHECK (stated_precision IN ('minute','second')),
            time_evidence jsonb NOT NULL DEFAULT '{}'::jsonb, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            first_synced_at timestamptz NOT NULL DEFAULT now(), synced_at timestamptz NOT NULL DEFAULT now(),
            CHECK (strategy_available_at = received_at)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS remote_analyst_messages_analyst_received_idx ON quant.remote_analyst_messages(remote_analyst_id, received_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS remote_analyst_messages_received_idx ON quant.remote_analyst_messages(received_at DESC)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.remote_analyst_message_versions (
            remote_message_id text NOT NULL REFERENCES quant.remote_analyst_messages(remote_message_id) ON DELETE CASCADE,
            remote_version text NOT NULL, content_hash text NOT NULL, payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (remote_message_id, remote_version, content_hash)
        )
    """)
    op.execute("ALTER TABLE quant.analyst_evidence ALTER COLUMN remote_report_id DROP NOT NULL")
    op.execute("ALTER TABLE quant.analyst_evidence ADD COLUMN IF NOT EXISTS remote_message_id text REFERENCES quant.remote_analyst_messages(remote_message_id) ON DELETE CASCADE")
    op.execute("ALTER TABLE quant.analyst_evidence ADD CONSTRAINT analyst_evidence_source_check CHECK ((remote_report_id IS NOT NULL)::integer + (remote_message_id IS NOT NULL)::integer = 1) NOT VALID")
    op.execute("ALTER TABLE quant.analyst_evidence VALIDATE CONSTRAINT analyst_evidence_source_check")
    op.execute("CREATE INDEX IF NOT EXISTS analyst_evidence_message_idx ON quant.analyst_evidence(remote_message_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS analyst_evidence_message_unique_idx ON quant.analyst_evidence(remote_message_id,evidence_key,content_sha256) WHERE remote_message_id IS NOT NULL")
    op.execute("ALTER TABLE quant.analyst_trade_actions ALTER COLUMN remote_report_id DROP NOT NULL")
    op.execute("ALTER TABLE quant.analyst_trade_actions ADD COLUMN IF NOT EXISTS remote_message_id text REFERENCES quant.remote_analyst_messages(remote_message_id) ON DELETE CASCADE")
    op.execute("ALTER TABLE quant.analyst_trade_actions ADD CONSTRAINT analyst_trade_actions_source_check CHECK ((remote_report_id IS NOT NULL)::integer + (remote_message_id IS NOT NULL)::integer = 1) NOT VALID")
    op.execute("ALTER TABLE quant.analyst_trade_actions VALIDATE CONSTRAINT analyst_trade_actions_source_check")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS analyst_trade_actions_message_unique_idx ON quant.analyst_trade_actions(remote_message_id,content_sha256) WHERE remote_message_id IS NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.analyst_trade_actions DROP CONSTRAINT IF EXISTS analyst_trade_actions_source_check")
    op.execute("ALTER TABLE quant.analyst_trade_actions DROP COLUMN IF EXISTS remote_message_id")
    op.execute("ALTER TABLE quant.analyst_trade_actions ALTER COLUMN remote_report_id SET NOT NULL")
    op.execute("ALTER TABLE quant.analyst_evidence DROP CONSTRAINT IF EXISTS analyst_evidence_source_check")
    op.execute("ALTER TABLE quant.analyst_evidence DROP COLUMN IF EXISTS remote_message_id")
    op.execute("ALTER TABLE quant.analyst_evidence ALTER COLUMN remote_report_id SET NOT NULL")
    op.execute("DROP TABLE IF EXISTS quant.remote_analyst_message_versions")
    op.execute("DROP TABLE IF EXISTS quant.remote_analyst_messages")
