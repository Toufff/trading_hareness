"""Register the existing strategy families as research-only contracts.

Revision ID: 20260814_0024
Revises: 20260814_0023
"""

from alembic import op
import json


revision = "20260814_0024"
down_revision = "20260814_0023"
branch_labels = None
depends_on = None


CONTRACTS = (
    ("watchlist_confirmation", "watchlist-confirmation-v4", "显式观察池二次确认", "entry/watch/reduce/exit"),
    ("upside_breakout_eac", "eac-v4", "首次扩张与承接确认", "entry/watch"),
    ("deep_reversal", "deep-reversal-v1", "深水反转与前收复", "watch"),
    ("green_reclaim", "green-reclaim-v1", "绿盘回收 VWAP/前收", "watch"),
    ("sector_surge", "sector-surge-v1", "板块资金轮动共振", "entry"),
    ("limit_linkage", "limit-linkage-v1", "涨停锚点精确成员关联", "watch"),
)


def upgrade() -> None:
    for strategy_key, version, label, signal_types in CONTRACTS:
        payload = json.dumps({
            "label_text": label, "signal_types": signal_types,
            "scope": "explicit_watchlist_or_research_candidate",
            "required_inputs": ["quote", "minute_path", "market_context"],
            "optional_inputs": ["order_book", "analyst_context"],
            "risk": {"market_gate": True, "tradability_gate": True, "portfolio_gate": True},
            "label_spec": {"fixed_horizons": ["5m", "15m", "30m", "close", "next_close"], "triple_barrier": "preregistered"},
            "alert": {"audience": "explicit_watchlist_only", "rearm": "clear_then_material_change"},
            "governance": {"status": "research_only", "live_effect": "none"},
        }).replace("'", "''")
        key = strategy_key.replace("'", "''")
        ver = version.replace("'", "''")
        op.execute(f"""INSERT INTO quant.strategy_contracts(strategy_key,strategy_version,status,contract)
                        VALUES('{key}','{ver}','research_only','{payload}'::jsonb)
                        ON CONFLICT(strategy_key,strategy_version) DO NOTHING""")


def downgrade() -> None:
    for strategy_key, version, _, _ in CONTRACTS:
        key = strategy_key.replace("'", "''")
        ver = version.replace("'", "''")
        op.execute(f"DELETE FROM quant.strategy_contracts WHERE strategy_key='{key}' AND strategy_version='{ver}'")
