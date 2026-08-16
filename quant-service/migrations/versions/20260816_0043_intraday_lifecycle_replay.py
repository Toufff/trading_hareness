"""Register lifecycle evidence and offline-only replay/calibration ledgers.

Revision ID: 20260816_0043
Revises: 20260816_0042
"""

from alembic import op


revision = "20260816_0043"
down_revision = "20260816_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO quant.strategy_registry(strategy_key,label,engine,version,configuration,status)
        VALUES(
            'watchlist_countertrend_rebound_lifecycle_v1','B浪反弹盘中承接与失效生命周期',
            'causal-countertrend-state-machine','lifecycle-v1',
            jsonb_build_object(
                'status','research_alert_only',
                'entry','confirmed_daily_state_plus_intraday_acceptance',
                'reduce','vwap_loss_plus_negative_momentum_and_flow_or_exact_peer_loss',
                'peer_mapping','same_taxonomy_and_sector_key_within_explicit_watchlist',
                't_plus_one','live_policy_gate','no_automatic_order',true
            ), 'experimental'
        ) ON CONFLICT(strategy_key) DO UPDATE SET
            label=EXCLUDED.label,engine=EXCLUDED.engine,version=EXCLUDED.version,
            configuration=EXCLUDED.configuration,status=EXCLUDED.status,updated_at=now()
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_replay_runs (
            replay_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            engine_version text NOT NULL,
            strategy_key text NOT NULL,
            strategy_version text NOT NULL,
            start_available_at timestamptz,
            end_available_at timestamptz,
            status text NOT NULL CHECK (status IN ('completed','blocked','failed')),
            input_hash text NOT NULL,
            trace_hash text,
            data_boundary jsonb NOT NULL DEFAULT '{}'::jsonb,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            error_message text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_replay_runs_strategy_created_idx
            ON quant.intraday_replay_runs(strategy_key,created_at DESC)
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.intraday_probability_calibrations (
            calibration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            calibration_version text NOT NULL,
            strategy_family text NOT NULL,
            signal_type text NOT NULL,
            horizon_key text NOT NULL,
            market_state text,
            setup_state text,
            status text NOT NULL CHECK (status IN ('insufficient_oof_evidence','diagnostic_only','eligible_for_manual_review','approved')),
            start_date date,
            end_date date,
            input_hash text NOT NULL,
            metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
            approved_by text,
            approved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((status <> 'approved') OR (approved_by IS NOT NULL AND approved_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS intraday_probability_calibrations_lookup_idx
            ON quant.intraday_probability_calibrations(strategy_family,signal_type,horizon_key,created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.intraday_probability_calibrations")
    op.execute("DROP TABLE IF EXISTS quant.intraday_replay_runs")
    op.execute("DELETE FROM quant.strategy_registry WHERE strategy_key='watchlist_countertrend_rebound_lifecycle_v1'")
