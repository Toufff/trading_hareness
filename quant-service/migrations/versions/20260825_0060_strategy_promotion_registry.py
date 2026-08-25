"""One live-promotion registry for research/shadow strategy contracts.

Revision ID: 20260825_0060
Revises: 20260825_0059
"""

from alembic import op


revision = "20260825_0060"
down_revision = "20260825_0059"
branch_labels = None
depends_on = None

# Every strategy currently declared in app/platform/strategy_registry.py.
# Each seeds disabled/zero-weight, mirroring the analyst_promotion_registry
# P0 safety default: only an explicit human approval can ever set a nonzero
# live weight, and only up to the same 10% ceiling.
SEED_STRATEGY_KEYS = (
    "intraday_watchlist_confirmation",
    "watchlist_main_wave_shadow",
    "countertrend_rebound_shadow",
    "ten_day_leader_rotation_shadow",
    "post_close_base_candidates",
    "post_close_limit_lift_pattern",
)


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS quant.strategy_promotion_registry (
            strategy_key text PRIMARY KEY,
            methodology_version text NOT NULL,
            status text NOT NULL CHECK (status IN ('collecting','eligible_for_review','approved','disabled','revoked')),
            approved_by text, approved_at timestamptz,
            max_live_weight numeric NOT NULL DEFAULT 0 CHECK (max_live_weight >= 0 AND max_live_weight <= 0.10),
            reason text NOT NULL DEFAULT '',
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((status = 'approved') = (approved_by IS NOT NULL AND approved_at IS NOT NULL))
        )
    """)
    # SEED_STRATEGY_KEYS is a fixed internal literal list, not external input;
    # no bind parameters are needed for this one-time seed.
    for key in SEED_STRATEGY_KEYS:
        op.execute(f"""
            INSERT INTO quant.strategy_promotion_registry(strategy_key,methodology_version,status,max_live_weight,reason,evidence)
            VALUES('{key}','unversioned','disabled',0,
                   'P0 safety default: only an explicitly approved research version may supply a nonzero live weight.',
                   '{{"live_strategy_effect":"none"}}'::jsonb)
            ON CONFLICT(strategy_key) DO NOTHING
        """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quant.strategy_promotion_registry")
