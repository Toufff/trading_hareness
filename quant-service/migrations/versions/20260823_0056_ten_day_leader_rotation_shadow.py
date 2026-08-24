"""Add the decoupled ten-day leader-rotation shadow projection.

Revision ID: 20260823_0056
Revises: 20260822_0055
"""

import json

from alembic import op


revision = "20260823_0056"
down_revision = "20260822_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.ten_day_leader_rotation_runs (
            run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_key text NOT NULL UNIQUE,
            as_of_date date NOT NULL,
            strategy_available_at timestamptz,
            model_version text NOT NULL,
            status text NOT NULL CHECK(status IN ('completed','partial','blocked')),
            scope text NOT NULL DEFAULT 'research_only_no_orders'
                CHECK(scope='research_only_no_orders'),
            source_status jsonb NOT NULL DEFAULT '{}'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS ten_day_leader_rotation_runs_date_idx
            ON quant.ten_day_leader_rotation_runs(as_of_date DESC,updated_at DESC);

        CREATE TABLE IF NOT EXISTS quant.ten_day_leader_rotation_candidates (
            run_id uuid NOT NULL REFERENCES quant.ten_day_leader_rotation_runs(run_id) ON DELETE CASCADE,
            board text NOT NULL CHECK(board IN ('main','growth','bj')),
            board_rank integer NOT NULL CHECK(board_rank BETWEEN 1 AND 30),
            symbol text NOT NULL REFERENCES quant.instruments(symbol),
            name text,
            ten_day_return_pct numeric NOT NULL,
            current_return_pct numeric NOT NULL,
            candidate_path text,
            shadow_state text NOT NULL,
            shadow_eligible boolean NOT NULL DEFAULT false,
            decision_eligible boolean NOT NULL DEFAULT false CHECK(NOT decision_eligible),
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            risk_flags jsonb NOT NULL DEFAULT '[]'::jsonb,
            source_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
            discovered_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY(run_id,symbol),
            UNIQUE(run_id,board,board_rank)
        );
        CREATE INDEX IF NOT EXISTS ten_day_leader_rotation_candidates_state_idx
            ON quant.ten_day_leader_rotation_candidates(run_id,shadow_state,board_rank);
    """)
    contract = json.dumps({
        "label_text": "十日排行榜龙头协同影子研究",
        "signal_types": "shadow_observation",
        "scope": "research_only_no_orders",
        "required_inputs": [
            "point_in_time_all_a_universe", "complete_adjusted_daily_cross_section",
            "ten_session_board_local_rank", "strategy_available_at",
        ],
        "optional_inputs": ["cycle_context", "exact_sector_peers", "minute_vwap", "minute_volume"],
        "governance": {
            "status": "research_only", "live_effect": "none", "orders": "prohibited",
            "promotion_required": True,
            "post_close_phase": "ranking_only_until_intraday_context_arrives",
        },
    }, ensure_ascii=False).replace("'", "''")
    op.execute(
        """INSERT INTO quant.strategy_contracts(strategy_key,strategy_version,status,contract)
             VALUES('ten_day_leader_rotation','ten-day-leader-vwap-coordination-shadow-v1',
                    'research_only','{contract}'::jsonb)
             ON CONFLICT(strategy_key,strategy_version) DO NOTHING""".format(contract=contract)
    )


def downgrade() -> None:
    op.execute("""
        DELETE FROM quant.strategy_contracts
         WHERE strategy_key='ten_day_leader_rotation'
           AND strategy_version='ten-day-leader-vwap-coordination-shadow-v1';
        DROP TABLE IF EXISTS quant.ten_day_leader_rotation_candidates;
        DROP TABLE IF EXISTS quant.ten_day_leader_rotation_runs;
    """)
