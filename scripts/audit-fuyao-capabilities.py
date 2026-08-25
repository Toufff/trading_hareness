#!/usr/bin/env python3
"""Run bounded, data-free live checks for every allowlisted Fuyao REST route.

The script prints one JSON record per capability and a final summary.  It never
prints credentials or provider payload rows.  Run it from ``quant-service`` so
the local ``app`` package is importable.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

for candidate in (Path.cwd(), Path(__file__).resolve().parents[1] / "quant-service", Path(__file__).resolve().parents[1]):
    if (candidate / "app").is_dir():
        sys.path.insert(0, str(candidate))
        break

from app.fuyao_catalog import FUYAO_PATHS
from app.fuyao_provider import FuyaoProviderError, fetch_envelope


STOCK = "600519.SH"
FUND = "025480.OF"
ETF = "159919.SZ"
SNAPSHOT_ETF = "588000.SH"
HOLDER_FUND = "007784.OF"
INDEX = "000300.SH"
START_MS = 1735689600000  # 2025-01-01T00:00:00Z
END_MS = 1767139200000  # 2025-12-31T00:00:00Z


def _base_cases() -> dict[str, dict[str, Any]]:
    recent_day = (datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=10)).isoformat()
    return {
        "a_share_prices_snapshot": {"thscodes": STOCK},
        "a_share_prices_historical": {"thscode": STOCK, "interval": "1d", "start": START_MS, "end": END_MS, "adjust": "forward"},
        "a_share_adjustment_factors": {"thscode": STOCK},
        "a_share_income_statements": {"thscode": STOCK, "period": "annual", "limit": 2},
        "a_share_balance_sheets": {"thscode": STOCK, "period": "annual", "limit": 2},
        "a_share_cash_flow_statements": {"thscode": STOCK, "period": "annual", "limit": 2},
        "a_share_financial_indicators": {"thscode": STOCK, "report": "2024-4"},
        "a_share_valuations_snapshot": {"thscodes": STOCK},
        "a_share_trading_days": {},
        "a_share_auction_snapshot": {"thscodes": STOCK, "stage": "final"},
        "a_share_auction_short_term_benchmark": {},
        "a_share_anomaly_analysis_list": {},
        "a_share_anomaly_analysis_stock": {"thscodes": STOCK},
        "a_share_dragon_tiger_list": {"board_type": "all"},
        "a_share_skyrocket_list": {"period": "day"},
        "a_share_hot_stock_list": {"period": "day"},
        "a_share_hot_stock_list_history": {"date": recent_day},
        "a_share_hot_stock_rank_trend": {"thscode": STOCK, "start_date": recent_day, "end_date": recent_day},
        "a_share_limit_up_pool": {"page": 1, "size": 1},
        "a_share_limit_down_pool": {"page": 1, "size": 1},
        "a_share_limit_break_pool": {"page": 1, "size": 1},
        "a_share_limit_up_ladder": {},
        "a_share_daily_k_10y_dump": {},
        "a_share_daily_k_10d_dump": {},
        "a_share_adjustment_factors_dump": {},
        "ths_index_list": {"tag": "cn_concept"},
        "ths_index_constituents": {"thscode": INDEX},
        "ths_index_prices_snapshot": {"thscodes": INDEX},
        "ths_index_prices_historical": {"thscode": INDEX, "interval": "1d", "start": START_MS, "end": END_MS},
        "ticker_search": {"q": "贵州茅台", "limit": 1},
        "ticker_list": {"asset_type": "a-share", "limit": 1, "offset": 0},
        "fund_company_detail": {"company_id": "00079099"},
        "fund_corporate_action_dividends": {"fund_type": "otc", "thscode": FUND},
        "fund_diagnostics_detail": {"fund_type": "otc", "thscode": FUND},
        "fund_financial_indicators": {"fund_type": "otc", "thscode": FUND},
        "fund_income_statements": {"fund_type": "otc", "thscode": FUND},
        "fund_balance_sheets": {"fund_type": "otc", "thscode": FUND},
        "fund_holder_detail": {"fund_type": "otc", "thscode": HOLDER_FUND, "merge_scope": "all"},
        "fund_holder_top": {"fund_type": "exchange", "thscode": "588000.SH", "limit": 1},
        "fund_portfolio_holdings": {"fund_type": "otc", "thscode": FUND},
        "fund_manager_investment_style": {"manager_id": "H002417139"},
        "fund_manager_performance": {"manager_id": "H002417139", "range": "year"},
        "fund_manager_experience": {"manager_id": "H002417139"},
        "fund_manager_detail": {"manager_id": "H002417139"},
        "fund_market_snapshot": {"thscode": SNAPSHOT_ETF},
        "fund_market_historical": {"thscode": ETF, "interval": "1d", "start": START_MS, "end": END_MS},
        "fund_news_article_list": {"fund_type": "otc", "thscode": FUND, "limit": 1},
        "fund_offerings_list": {"subscribe": "active"},
        "fund_performance_nav": {"fund_type": "otc", "thscode": FUND, "range": "year", "nav_type": "unit,adj"},
        "fund_performance_returns": {"fund_type": "otc", "thscode": FUND},
        "fund_performance_indicators_historical": {"fund_type": "otc", "thscode": FUND, "start": START_MS, "end": END_MS},
        "fund_performance_drawdowns": {"fund_type": "otc", "thscode": FUND},
        "fund_portfolio_stock_history": {"fund_type": "otc", "thscode": FUND, "report_type": "quarter", "end_date": "2026-06-30"},
        "fund_portfolio_bond_history": {"fund_type": "otc", "thscode": FUND, "report_type": "quarter", "end_date": "2026-06-30"},
        "fund_portfolio_stock_report_dates": {"fund_type": "otc", "thscode": FUND},
        "fund_portfolio_bond_report_dates": {"fund_type": "otc", "thscode": FUND},
        "fund_portfolio_asset_allocation": {"fund_type": "otc", "thscode": FUND},
        "fund_portfolio_industry_allocation": {"fund_type": "otc", "thscode": FUND},
        "fund_profile_detail": {"fund_type": "otc", "thscode": FUND},
    }


def _result_shape(data: dict[str, Any]) -> tuple[str, int | None]:
    items = data.get("item")
    if isinstance(items, list):
        return ("valid_empty" if not items else "success", len(items))
    return "success", None


async def audit(selected: set[str], delay_seconds: float) -> list[dict[str, Any]]:
    cases = _base_cases()
    if set(cases) != set(FUYAO_PATHS):
        raise RuntimeError("audit cases do not exactly match the Fuyao allow-list")
    results: list[dict[str, Any]] = []
    for capability, path in FUYAO_PATHS.items():
        if selected and capability not in selected:
            continue
        try:
            envelope = await fetch_envelope(capability, cases[capability])
            state, item_count = _result_shape(envelope["data"])
            result = {
                "capability": capability, "path": path, "state": state,
                "code": envelope["code"], "request_id": envelope["request_id"],
                "item_count": item_count,
            }
        except FuyaoProviderError as error:
            result = {
                "capability": capability, "path": path, "state": "failed",
                "code": error.code, "request_id": error.request_id,
                "error": str(error),
            }
        results.append(result)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--delay-seconds", type=float, default=0.1)
    args = parser.parse_args()
    selected = set(args.only)
    unknown = selected - set(FUYAO_PATHS)
    if unknown:
        parser.error(f"unknown capabilities: {', '.join(sorted(unknown))}")
    results = asyncio.run(audit(selected, max(0.0, min(2.0, args.delay_seconds))))
    counts: dict[str, int] = {}
    for item in results:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    print(json.dumps({"summary": counts, "count": len(results)}, ensure_ascii=False, separators=(",", ":")))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
