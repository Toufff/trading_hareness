"""Add stable pool-event identities and multiscale market-flow features.

Revision ID: 20260815_0032
Revises: 20260815_0031
"""

from alembic import op


revision = "20260815_0032"
down_revision = "20260815_0031"
branch_labels = None
depends_on = None


POOL_TYPES = (
    "limit_up_pool", "previous_limit_pool", "limit_open_pool",
    "limit_down_pool", "sub_new_limit_pool", "strong_pool",
)


def upgrade() -> None:
    op.execute("ALTER TABLE quant.market_events ADD COLUMN IF NOT EXISTS event_identity_key text")
    pool_types = ",".join(f"'{value}'" for value in POOL_TYPES)
    op.execute(f"""
        WITH ranked AS (
            SELECT event_id,
                   row_number() OVER (
                       PARTITION BY source,event_type,symbol,
                                    (occurred_at AT TIME ZONE 'Asia/Shanghai')::date
                       ORDER BY available_at,created_at,event_id
                   ) AS position
              FROM quant.market_events
             WHERE event_type IN ({pool_types})
        )
        DELETE FROM quant.market_events event
         USING ranked
         WHERE event.event_id=ranked.event_id AND ranked.position>1
    """)
    op.execute(f"""
        UPDATE quant.market_events
           SET event_identity_key=source || ':' || event_type || ':' || symbol || ':' ||
               to_char(occurred_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD')
         WHERE event_type IN ({pool_types}) AND symbol IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS market_events_identity_unique_idx
            ON quant.market_events(event_identity_key) WHERE event_identity_key IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.market_flow_feature_snapshots (
            feature_key text PRIMARY KEY,
            exchange_date date NOT NULL,
            cadence text NOT NULL CHECK (cadence IN ('minute','midday','close')),
            observed_at timestamptz NOT NULL,
            source_snapshot_minute timestamptz,
            status text NOT NULL CHECK (status IN ('ready','partial','insufficient')),
            market_state text NOT NULL,
            concept_count integer NOT NULL DEFAULT 0 CHECK (concept_count >= 0),
            concept_positive_ratio numeric,
            concept_median_flow numeric,
            concept_mean_change_pct numeric,
            five_minute_positive_ratio_delta numeric,
            session_positive_ratio_delta numeric,
            afternoon_repair_strength numeric,
            market_amount numeric,
            market_volume numeric,
            amount_change_pct numeric,
            volume_change_pct numeric,
            advancer_ratio numeric,
            features jsonb NOT NULL DEFAULT '{}'::jsonb,
            quality_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS market_flow_feature_time_idx
            ON quant.market_flow_feature_snapshots(exchange_date DESC,cadence,observed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS quant.market_flow_feature_time_idx")
    op.execute("DROP TABLE IF EXISTS quant.market_flow_feature_snapshots")
    op.execute("DROP INDEX IF EXISTS quant.market_events_identity_unique_idx")
    op.execute("ALTER TABLE quant.market_events DROP COLUMN IF EXISTS event_identity_key")
