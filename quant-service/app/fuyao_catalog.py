"""Declared Fuyao/THS REST capability catalogue.

The catalogue is an explicit allow-list, not a claim that every supplier route
has been observed in this deployment.  It keeps the broad vendor surface
available through one validated adapter without granting arbitrary URL access.
"""

from __future__ import annotations

from typing import Final


# Pinned to the official HiThink-Tech/Financial-API capability snapshot used
# for this allow-list audit.  This is contract provenance, not a claim that the
# configured account is entitled to every route.
FUYAO_OFFICIAL_REPOSITORY_REVISION: Final[str] = "9dbef74d2ce535857e610eec265bcb9302942d48"
FUYAO_OFFICIAL_REST_COUNT: Final[int] = 59
FUYAO_OFFICIAL_MCP_TOOL_COUNT: Final[int] = 55


def _entry(key: str, path: str, category: str, frequency: str) -> dict[str, str]:
    return {"key": key, "path": path, "category": category, "frequency": frequency,
            "decision_eligible": "false", "status": "declared"}


_A_SHARE: Final[tuple[tuple[str, str, str], ...]] = (
    ("a_share_prices_snapshot", "/api/a-share/prices/snapshot", "realtime"),
    ("a_share_prices_historical", "/api/a-share/prices/historical", "daily"),
    ("a_share_adjustment_factors", "/api/a-share/corporate-actions/adjustment-factors", "daily"),
    ("a_share_income_statements", "/api/a-share/financials/income-statements", "quarterly"),
    ("a_share_balance_sheets", "/api/a-share/financials/balance-sheets", "quarterly"),
    ("a_share_cash_flow_statements", "/api/a-share/financials/cash-flow-statements", "quarterly"),
    ("a_share_financial_indicators", "/api/a-share/financials/indicators", "quarterly"),
    ("a_share_valuations_snapshot", "/api/a-share/valuations/snapshot", "realtime"),
    ("a_share_trading_days", "/api/a-share/calendar/trading-days", "calendar"),
    ("a_share_auction_snapshot", "/api/a-share/auction/snapshot", "auction"),
    ("a_share_auction_short_term_benchmark", "/api/a-share/auction/short-term-benchmark", "auction"),
    ("a_share_anomaly_analysis_list", "/api/a-share/special-data/anomaly-analysis-list", "event"),
    ("a_share_anomaly_analysis_stock", "/api/a-share/special-data/anomaly-analysis-stock", "event"),
    ("a_share_dragon_tiger_list", "/api/a-share/special-data/dragon-tiger-list", "event"),
    ("a_share_skyrocket_list", "/api/a-share/special-data/skyrocket-list", "event"),
    ("a_share_hot_stock_list", "/api/a-share/special-data/hot-stock-list", "event"),
    ("a_share_hot_stock_list_history", "/api/a-share/special-data/hot-stock-list-history", "daily"),
    ("a_share_hot_stock_rank_trend", "/api/a-share/special-data/hot-stock-rank-trend", "daily"),
    ("a_share_limit_up_pool", "/api/a-share/special-data/limit-up-pool", "event"),
    ("a_share_limit_down_pool", "/api/a-share/special-data/limit-down-pool", "event"),
    ("a_share_limit_break_pool", "/api/a-share/special-data/limit-break-pool", "event"),
    ("a_share_limit_up_ladder", "/api/a-share/special-data/limit-up-ladder", "event"),
)
_DUMPS: Final[tuple[tuple[str, str, str], ...]] = (
    ("a_share_daily_k_10y_dump", "/api/dump/market-dumps/daily-k/download-url", "bulk_download"),
    ("a_share_daily_k_10d_dump", "/api/dump/market-dumps/daily-k-10d/download-url", "bulk_download"),
    ("a_share_adjustment_factors_dump", "/api/dump/market-dumps/adjustment-factors/download-url", "bulk_download"),
)
_INDEX: Final[tuple[tuple[str, str, str], ...]] = (
    ("ths_index_list", "/api/a-share-index/catalog/ths-index-list", "reference"),
    ("ths_index_constituents", "/api/a-share-index/constituents/ths-stock-list", "reference"),
    ("ths_index_prices_snapshot", "/api/a-share-index/prices/snapshot", "realtime"),
    ("ths_index_prices_historical", "/api/a-share-index/prices/historical", "daily"),
    ("ticker_search", "/api/meta/tickers/search", "reference"),
    ("ticker_list", "/api/meta/tickers/list", "reference"),
)
_FUND: Final[tuple[tuple[str, str, str], ...]] = (
    ("fund_company_detail", "/api/fund/companies/detail", "reference"),
    ("fund_corporate_action_dividends", "/api/fund/corporate-actions/dividends", "event"),
    ("fund_diagnostics_detail", "/api/fund/diagnostics/detail", "daily"),
    ("fund_financial_indicators", "/api/fund/financials/indicators", "quarterly"),
    ("fund_income_statements", "/api/fund/financials/income-statements", "quarterly"),
    ("fund_balance_sheets", "/api/fund/financials/balance-sheets", "quarterly"),
    ("fund_holder_detail", "/api/fund/holders/detail", "quarterly"),
    ("fund_holder_top", "/api/fund/holders/top", "quarterly"),
    ("fund_portfolio_holdings", "/api/fund/portfolio/holdings", "quarterly"),
    ("fund_manager_investment_style", "/api/fund/managers/investment-style", "reference"),
    ("fund_manager_performance", "/api/fund/managers/performance", "daily"),
    ("fund_manager_experience", "/api/fund/managers/experience", "reference"),
    ("fund_manager_detail", "/api/fund/managers/detail", "reference"),
    ("fund_market_snapshot", "/api/fund/market/snapshot", "daily"),
    ("fund_market_historical", "/api/fund/market/historical", "daily"),
    ("fund_news_article_list", "/api/fund/news/article-list", "event"),
    ("fund_offerings_list", "/api/fund/offerings/list", "event"),
    ("fund_performance_nav", "/api/fund/performance/nav", "daily"),
    ("fund_performance_returns", "/api/fund/performance/returns", "daily"),
    ("fund_performance_indicators_historical", "/api/fund/performance/indicators-historical", "daily"),
    ("fund_performance_drawdowns", "/api/fund/performance/drawdowns", "daily"),
    ("fund_portfolio_stock_history", "/api/fund/portfolio/stock-history", "quarterly"),
    ("fund_portfolio_bond_history", "/api/fund/portfolio/bond-history", "quarterly"),
    ("fund_portfolio_stock_report_dates", "/api/fund/portfolio/stock-report-dates", "quarterly"),
    ("fund_portfolio_bond_report_dates", "/api/fund/portfolio/bond-report-dates", "quarterly"),
    ("fund_portfolio_asset_allocation", "/api/fund/portfolio/asset-allocation", "quarterly"),
    ("fund_portfolio_industry_allocation", "/api/fund/portfolio/industry-allocation", "quarterly"),
    ("fund_profile_detail", "/api/fund/profile/detail", "reference"),
)

FUYAO_CATALOG: Final[tuple[dict[str, str], ...]] = tuple(
    _entry(key, path, "a_share", frequency) for key, path, frequency in _A_SHARE
) + tuple(_entry(key, path, "market_dump", frequency) for key, path, frequency in _DUMPS) + tuple(_entry(key, path, "ths_index_or_meta", frequency) for key, path, frequency in _INDEX) + tuple(
    _entry(key, path, "fund", frequency) for key, path, frequency in _FUND
)
FUYAO_PATHS: Final[dict[str, str]] = {item["key"]: item["path"] for item in FUYAO_CATALOG}


def _params(allowed: str = "", required: str = "") -> dict[str, tuple[str, ...]]:
    return {
        "allowed": tuple(value for value in allowed.split() if value),
        "required": tuple(value for value in required.split() if value),
    }


# Query names are copied from the official REST/Python contracts.  Keeping this
# independent from response schemas lets the proxy reject parameter typos while
# still returning supplier data without silently remapping fields.
FUYAO_QUERY_PARAMS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "a_share_prices_snapshot": _params("thscodes limit offset"),
    "a_share_prices_historical": _params("thscode interval start end adjust offset", "thscode start end"),
    "a_share_adjustment_factors": _params("thscode from to", "thscode"),
    "a_share_income_statements": _params("thscode period limit start end", "thscode"),
    "a_share_balance_sheets": _params("thscode period limit start end", "thscode"),
    "a_share_cash_flow_statements": _params("thscode period limit start end", "thscode"),
    "a_share_financial_indicators": _params("thscode report", "thscode report"),
    "a_share_valuations_snapshot": _params("thscodes", "thscodes"),
    "a_share_trading_days": _params(),
    "a_share_auction_snapshot": _params("thscodes stage", "thscodes"),
    "a_share_auction_short_term_benchmark": _params("date"),
    "a_share_anomaly_analysis_list": _params("tag_codes"),
    "a_share_anomaly_analysis_stock": _params("thscodes", "thscodes"),
    "a_share_dragon_tiger_list": _params("board_type date"),
    "a_share_skyrocket_list": _params("period"),
    "a_share_hot_stock_list": _params("period"),
    "a_share_hot_stock_list_history": _params("date", "date"),
    "a_share_hot_stock_rank_trend": _params("thscode start_date end_date", "thscode start_date end_date"),
    "a_share_limit_up_pool": _params("date_ms page size sort_field sort_dir"),
    "a_share_limit_down_pool": _params("date_ms page size sort_field sort_dir"),
    "a_share_limit_break_pool": _params("date_ms page size sort_field sort_dir"),
    "a_share_limit_up_ladder": _params(),
    "a_share_daily_k_10y_dump": _params(),
    "a_share_daily_k_10d_dump": _params(),
    "a_share_adjustment_factors_dump": _params(),
    "ths_index_list": _params("tag"),
    "ths_index_constituents": _params("thscode", "thscode"),
    "ths_index_prices_snapshot": _params("thscodes limit offset", "thscodes"),
    "ths_index_prices_historical": _params("thscode interval start end", "thscode start end"),
    "ticker_search": _params("q exchange asset_type limit", "q"),
    "ticker_list": _params("exchange asset_type limit offset"),
    "fund_company_detail": _params("company_id", "company_id"),
    "fund_corporate_action_dividends": _params("fund_type thscode", "fund_type thscode"),
    "fund_diagnostics_detail": _params("fund_type thscode", "fund_type thscode"),
    "fund_financial_indicators": _params("fund_type thscode", "fund_type thscode"),
    "fund_income_statements": _params("fund_type thscode", "fund_type thscode"),
    "fund_balance_sheets": _params("fund_type thscode", "fund_type thscode"),
    "fund_holder_detail": _params("fund_type thscode merge_scope", "fund_type thscode"),
    "fund_holder_top": _params("fund_type thscode limit", "fund_type thscode"),
    "fund_portfolio_holdings": _params("fund_type thscode", "fund_type thscode"),
    "fund_manager_investment_style": _params("manager_id", "manager_id"),
    "fund_manager_performance": _params("manager_id range", "manager_id range"),
    "fund_manager_experience": _params("manager_id", "manager_id"),
    "fund_manager_detail": _params("manager_id", "manager_id"),
    "fund_market_snapshot": _params("thscode", "thscode"),
    "fund_market_historical": _params("thscode interval start end", "thscode start end"),
    "fund_news_article_list": _params("fund_type thscode limit offset", "fund_type thscode"),
    "fund_offerings_list": _params("subscribe", "subscribe"),
    "fund_performance_nav": _params("fund_type thscode range nav_type", "fund_type thscode"),
    "fund_performance_returns": _params("fund_type thscode", "fund_type thscode"),
    "fund_performance_indicators_historical": _params("fund_type thscode start end", "fund_type thscode start end"),
    "fund_performance_drawdowns": _params("fund_type thscode", "fund_type thscode"),
    "fund_portfolio_stock_history": _params("fund_type thscode report_type end_date", "fund_type thscode report_type end_date"),
    "fund_portfolio_bond_history": _params("fund_type thscode report_type end_date", "fund_type thscode report_type end_date"),
    "fund_portfolio_stock_report_dates": _params("fund_type thscode report_type", "fund_type thscode"),
    "fund_portfolio_bond_report_dates": _params("fund_type thscode report_type", "fund_type thscode"),
    "fund_portfolio_asset_allocation": _params("fund_type thscode", "fund_type thscode"),
    "fund_portfolio_industry_allocation": _params("fund_type thscode", "fund_type thscode"),
    "fund_profile_detail": _params("fund_type thscode", "fund_type thscode"),
}


def catalog_items() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in FUYAO_CATALOG:
        params = FUYAO_QUERY_PARAMS.get(item["key"], _params())
        result.append({**item, "allowed_params": list(params["allowed"]), "required_params": list(params["required"])})
    return result


def catalog_contract() -> dict[str, object]:
    """Describe exact static coverage without conflating it with live access."""
    paths = tuple(FUYAO_PATHS.values())
    parameter_contract_complete = set(FUYAO_QUERY_PARAMS) == set(FUYAO_PATHS)
    return {
        "official_repository_revision": FUYAO_OFFICIAL_REPOSITORY_REVISION,
        "official_rest_count": FUYAO_OFFICIAL_REST_COUNT,
        "allowlisted_rest_count": len(paths),
        "unique_rest_path_count": len(set(paths)),
        "rest_contract_complete": len(paths) == FUYAO_OFFICIAL_REST_COUNT == len(set(paths)),
        "parameter_contract_complete": parameter_contract_complete,
        "official_mcp_tool_count": FUYAO_OFFICIAL_MCP_TOOL_COUNT,
        "entry_points": {
            "rest": "integrated_allowlist",
            "mcp": "alternate_official_access_surface",
            "cli_python_marketdb_skill": "alternate_official_workflow_surfaces",
        },
        "public_capability_limits": [
            "no_minute_bars", "no_tick", "no_level2", "no_overseas_market",
            "no_macro", "no_announcement_originals", "no_research_reports",
        ],
    }


__all__ = [
    "FUYAO_CATALOG", "FUYAO_OFFICIAL_MCP_TOOL_COUNT",
    "FUYAO_OFFICIAL_REPOSITORY_REVISION", "FUYAO_OFFICIAL_REST_COUNT",
    "FUYAO_PATHS", "FUYAO_QUERY_PARAMS", "catalog_contract", "catalog_items",
]
