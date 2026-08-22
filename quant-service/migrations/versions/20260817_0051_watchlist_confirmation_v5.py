"""Register the opening-gap-aware watchlist confirmation contract.

Revision ID: 20260817_0051
Revises: 20260816_0050
"""

import json

from alembic import op


revision = "20260817_0051"
down_revision = "20260816_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    contract = json.dumps({
        "label_text": "显式观察池二次确认（含开盘跳空观察）",
        "signal_types": "entry/watch/reduce/exit",
        "scope": "explicit_watchlist_only",
        "required_inputs": ["quote", "minute_path", "market_context"],
        "optional_inputs": ["order_book", "analyst_context"],
        "risk": {"market_gate": True, "tradability_gate": True, "portfolio_gate": True},
        "label_spec": {"fixed_horizons": ["5m", "15m", "30m", "close", "next_close"],
                       "triple_barrier": "preregistered"},
        "alert": {"audience": "explicit_watchlist_only", "rearm": "clear_then_material_change"},
        "governance": {"status": "research_only", "live_effect": "none",
                       "opening_gap": "watch_only_until_minute_confirmation"},
    }, ensure_ascii=False).replace("'", "''")
    op.execute(
        """INSERT INTO quant.strategy_contracts(strategy_key,strategy_version,status,contract)
             VALUES(
                 'watchlist_confirmation', 'watchlist-confirmation-v5', 'research_only',
                 '{contract}'::jsonb
             ) ON CONFLICT(strategy_key,strategy_version) DO NOTHING""".format(contract=contract)
    )


def downgrade() -> None:
    op.execute(
        """DELETE FROM quant.strategy_contracts
             WHERE strategy_key='watchlist_confirmation'
               AND strategy_version='watchlist-confirmation-v5'"""
    )
