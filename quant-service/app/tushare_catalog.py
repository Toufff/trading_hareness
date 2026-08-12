"""The customer's enabled Tushare-Pro compatible surface.

This file is an allow-list, not a promise that every endpoint is pulled every
day.  Calls go through bounded ``/tushare/fetch`` and are retained raw until a
domain adapter explicitly normalizes them.
"""

from __future__ import annotations

from typing import Any, Final

from .capability_registry import catalog_metadata
from .tushare_official import OFFICIAL_EXTENSIONS, default_probe_params, official_metadata


def _group(name: str, values: tuple[str, ...]) -> dict[str, str]:
    return {value: name for value in values}


SUPPLIER_109_CATALOG: Final[dict[str, str]] = (
    _group("基础数据", (
        "trade_cal", "stock_basic", "etf_basic", "etf_index", "opt_basic", "fund_basic", "index_basic", "namechange", "new_share",
    ))
    | _group("行情数据", (
        "daily", "weekly", "monthly", "adj_factor", "daily_basic", "stk_limit", "suspend_d", "fund_daily", "fund_adj",
        "etf_share_size", "index_daily", "index_dailybasic", "opt_daily", "cb_daily", "hk_hold",
    ))
    | _group("财务和宏观", (
        "income", "income_vip", "balancesheet", "balancesheet_vip", "cashflow", "cashflow_vip", "forecast", "forecast_vip",
        "express", "express_vip", "dividend", "fina_indicator", "fina_indicator_vip", "fina_audit", "fina_mainbz",
        "fina_mainbz_vip", "disclosure_date", "eco_cal", "shibor", "shibor_quote", "shibor_lpr", "libor", "hibor",
        "wz_index", "gz_index", "cn_gdp", "cn_cpi", "cn_ppi", "cn_m", "sf_month", "cn_pmi", "us_tycr", "us_trycr",
        "us_tbr", "us_tltr", "us_trltr",
    ))
    | _group("名单和交易", (
        "stock_st", "stock_hsgt", "top10_holders", "top10_floatholders", "pledge_stat", "pledge_detail", "repurchase",
        "share_float", "block_trade", "stk_holdernumber", "stk_holdertrade", "top_list", "top_inst", "margin",
        "margin_detail", "margin_secs",
    ))
    | _group("特色数据", (
        "cyq_perf", "cyq_chips", "stk_factor", "stk_factor_pro", "report_rc", "broker_recommend", "stk_surv", "moneyflow",
        "moneyflow_ths", "moneyflow_dc", "moneyflow_ind_ths", "moneyflow_ind_dc", "moneyflow_mkt_dc", "moneyflow_hsgt",
        "limit_list_ths", "limit_list_d", "limit_step", "limit_cpt_list", "ths_hot", "dc_hot", "hm_list", "hm_detail",
        "ths_index", "ths_daily", "ths_member", "dc_index", "dc_daily", "dc_member", "tdx_index", "tdx_daily",
        "tdx_member", "kpl_list", "kpl_concept_cons",
    ))
)


AUDITED_ADDITIONS_CATALOG: Final[dict[str, str]] = (
    # Not present in the supplier's written 109-item list, but the auction
    # routes were empirically accepted on 2026-08-09. Real-time routes are
    # declared for both vendors and remain pending until a live-session probe.
    _group("集合竞价（实测开放）", (
        "stk_auction", "stk_auction_o", "stk_auction_c",
    ))
    | _group("实时行情（仅交易时段验证）", (
        "rt_min", "rt_idx_min", "rt_etf_min", "rt_fut_min",
    ))
)


TUSHARE_CATALOG: Final[dict[str, str]] = (
    SUPPLIER_109_CATALOG
    | AUDITED_ADDITIONS_CATALOG
    | {api_name: spec.group for api_name, spec in OFFICIAL_EXTENSIONS.items()}
)


CORE_NORMALIZED_APIS: Final[frozenset[str]] = frozenset({
    "trade_cal", "stock_basic", "daily", "index_daily", "adj_factor", "daily_basic", "stk_limit", "suspend_d",
})


def catalog_items() -> list[dict[str, Any]]:
    return [
        {
            "api_name": api_name,
            "group": group,
            "normalized": api_name in CORE_NORMALIZED_APIS,
            **official_metadata(api_name),
            **catalog_metadata(api_name),
            "sample_params": default_probe_params(api_name),
        }
        for api_name, group in sorted(TUSHARE_CATALOG.items())
    ]


def catalog_counts() -> dict[str, int]:
    items = catalog_items()
    return {
        "total": len(items),
        "supplier_109": len(SUPPLIER_109_CATALOG),
        "audited_additions": len(AUDITED_ADDITIONS_CATALOG),
        "official_extensions": len(OFFICIAL_EXTENSIONS),
        "points_at_or_below_15000": sum(
            1 for item in items
            if item["permission_model"] == "points" and item["min_points"] is not None and item["min_points"] <= 15000
        ),
        "market_hours_only": sum(1 for item in items if item["request_policy"] == "market_hours_only"),
        "offline_files_only": sum(1 for item in items if item["request_policy"] == "offline_files_only"),
    }
