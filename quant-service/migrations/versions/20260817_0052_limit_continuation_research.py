"""Register the post-close limit-continuation research screen.

Revision ID: 20260817_0052
Revises: 20260817_0051
"""

import json

from alembic import op


revision = "20260817_0052"
down_revision = "20260817_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    contract = json.dumps({
        "label_text": "封板连板延续观察",
        "signal_types": "watch",
        "scope": "post_close_limit_pool_only",
        "required_inputs": ["final_limit_pool", "ladder_tag", "final_seal", "free_float"],
        "optional_inputs": ["open_board_count", "sector_context", "auction", "minute_path"],
        "risk": {"market_gate": True, "tradability_gate": True, "portfolio_gate": True},
        "alert": {"audience": "research_review_only", "timing": "next_session_before_open"},
        "governance": {
            "status": "research_only", "live_effect": "none", "orders": "prohibited",
            "data_boundary": "post_close_final_limit_pool_never_intraday",
        },
    }, ensure_ascii=False).replace("'", "''")
    op.execute(
        """INSERT INTO quant.strategy_contracts(strategy_key,strategy_version,status,contract)
             VALUES('limit_continuation', 'limit-continuation-v1', 'research_only', '{contract}'::jsonb)
             ON CONFLICT(strategy_key,strategy_version) DO NOTHING""".format(contract=contract)
    )


def downgrade() -> None:
    op.execute(
        """DELETE FROM quant.strategy_contracts
             WHERE strategy_key='limit_continuation' AND strategy_version='limit-continuation-v1'"""
    )
