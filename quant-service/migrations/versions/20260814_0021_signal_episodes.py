"""Persist signal episode identity and material lifecycle state.

Revision ID: 20260814_0021
Revises: 20260814_0020
"""

from alembic import op


revision = "20260814_0021"
down_revision = "20260814_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_signal_episodes (
            episode_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            symbol text NOT NULL REFERENCES quant.instruments(symbol) ON DELETE CASCADE,
            strategy_key text NOT NULL,
            strategy_version text NOT NULL,
            direction smallint NOT NULL CHECK (direction IN (-1, 1)),
            session_date date NOT NULL,
            state text NOT NULL CHECK (state IN ('active','cleared','expired','invalidated')),
            stage text NOT NULL DEFAULT 'detected',
            material_state_hash text NOT NULL,
            first_observed_at timestamptz NOT NULL,
            last_observed_at timestamptz NOT NULL,
            clear_at timestamptz,
            clear_reason text,
            rearm_count integer NOT NULL DEFAULT 0 CHECK (rearm_count >= 0),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        ALTER TABLE quant.intraday_signal_events
            ADD COLUMN IF NOT EXISTS episode_id uuid REFERENCES quant.intraday_signal_episodes(episode_id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS material_state_hash text,
            ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'detected'
    """)
    op.execute("CREATE INDEX IF NOT EXISTS intraday_signal_episodes_active_idx ON quant.intraday_signal_episodes(symbol,strategy_key,state,last_observed_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS intraday_signal_events_episode_idx ON quant.intraday_signal_events(episode_id,observed_at DESC)")


def downgrade() -> None:
    op.execute("ALTER TABLE quant.intraday_signal_events DROP COLUMN IF EXISTS stage")
    op.execute("ALTER TABLE quant.intraday_signal_events DROP COLUMN IF EXISTS material_state_hash")
    op.execute("ALTER TABLE quant.intraday_signal_events DROP COLUMN IF EXISTS episode_id")
    op.execute("DROP TABLE IF EXISTS quant.intraday_signal_episodes")
