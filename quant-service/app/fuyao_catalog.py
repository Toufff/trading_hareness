"""Declared Fuyao/THS REST capability catalogue.

The catalogue is an explicit allow-list, not a claim that every supplier route
has been observed in this deployment.  It keeps the broad vendor surface
available through one validated adapter without granting arbitrary URL access.
"""

from __future__ import annotations

from typing import Final


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


def catalog_items() -> list[dict[str, str]]:
    return [dict(item) for item in FUYAO_CATALOG]


__all__ = ["FUYAO_CATALOG", "FUYAO_PATHS", "catalog_items"]
