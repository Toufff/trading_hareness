"""Official Tushare capability metadata used by the provider audit layer.

The supplier's 109-item bundle, official point-based APIs, separately licensed
real-time products, and bulk minute-file delivery are different contracts.
Keeping those facts explicit prevents a catalog entry from being mistaken for
proof that a provider returned usable market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final, Literal


PermissionModel = Literal["points", "separate_permission", "provider_contract", "offline_delivery"]
RequestPolicy = Literal["online_bounded", "market_hours_only", "offline_files_only"]


@dataclass(frozen=True)
class OfficialApi:
    group: str
    doc_id: int | None = None
    permission_model: PermissionModel = "points"
    min_points: int | None = None
    frequency: str = "periodic"
    request_policy: RequestPolicy = "online_bounded"
    model_role: str = "raw_evidence"
    priority: str = "P2"
    required_params: tuple[str, ...] = ()
    probe_profile: str = "manual"

    @property
    def official_doc_url(self) -> str | None:
        return f"https://tushare.pro/document/2?doc_id={self.doc_id}" if self.doc_id else None


def _api(group: str, doc_id: int | None = None, *, permission_model: PermissionModel = "points",
         min_points: int | None = None, frequency: str = "periodic",
         request_policy: RequestPolicy = "online_bounded", model_role: str = "raw_evidence",
         priority: str = "P2", required_params: tuple[str, ...] = (),
         probe_profile: str = "manual") -> OfficialApi:
    return OfficialApi(group, doc_id, permission_model, min_points, frequency, request_policy,
                       model_role, priority, required_params, probe_profile)


REALTIME_MARKET_HOURS_APIS: Final[frozenset[str]] = frozenset({
    "rt_k", "rt_min", "rt_min_daily",
    "rt_etf_k", "rt_etf_min", "rt_etf_min_daily", "rt_etf_sz_iopv",
    "rt_idx_k", "rt_idx_min", "rt_idx_min_daily", "rt_sw_k",
    "rt_fut_min", "rt_fut_min_daily",
})

# The provider explicitly supplies historical minute data as files.  These
# names remain visible for coverage comparison but are rejected by online
# fetch routes so large backfills cannot consume gateway bandwidth or memory.
HISTORICAL_MINUTE_APIS: Final[frozenset[str]] = frozenset({
    "stk_mins", "etf_mins", "sw_mins", "fut_mins", "fut_tick", "opt_mins",
})


OFFICIAL_EXTENSIONS: Final[dict[str, OfficialApi]] = {
    # Company/reference APIs missing from the supplier's written 109 list.
    "stock_company": _api("官方扩展·公司与名单", 112, min_points=120, frequency="reference_periodic",
                          model_role="issuer_profile", priority="P1", probe_profile="company_exchange"),
    "stk_managers": _api("官方扩展·公司与名单", 193, min_points=2000, frequency="event",
                         model_role="governance", probe_profile="stock_range"),
    "stk_rewards": _api("官方扩展·公司与名单", 194, min_points=2000, frequency="event",
                        model_role="governance", probe_profile="stock_range"),
    "bse_mapping": _api("官方扩展·公司与名单", 375, min_points=2000, frequency="reference_periodic",
                        model_role="instrument_mapping", probe_profile="no_params"),
    "bak_basic": _api("官方扩展·公司与名单", 262, min_points=5000, frequency="daily",
                      model_role="cross_section_reference", priority="P1", probe_profile="trade_date"),
    "st": _api("官方扩展·公司与名单", 423, min_points=6000, frequency="daily",
               model_role="risk_control", priority="P0", probe_profile="stock_only"),
    "stk_premarket": _api("官方扩展·公司与名单", 329, permission_model="separate_permission",
                          frequency="daily", model_role="share_structure", priority="P1",
                          probe_profile="trade_date"),

    # Equity market, north/southbound, event and risk APIs.
    "stk_weekly_monthly": _api("官方扩展·行情与交易", 336, min_points=2000, frequency="weekly_monthly",
                               model_role="price_history", priority="P1", probe_profile="stock_range"),
    "stk_week_month_adj": _api("官方扩展·行情与交易", 365, min_points=2000, frequency="weekly_monthly",
                               model_role="adjusted_price_history", priority="P1", probe_profile="stock_range"),
    "hsgt_top10": _api("官方扩展·行情与交易", 48, min_points=2000, frequency="daily",
                       model_role="northbound_flow", priority="P1", probe_profile="trade_date"),
    "ggt_top10": _api("官方扩展·行情与交易", 49, min_points=2000, frequency="daily",
                      model_role="southbound_flow", probe_profile="trade_date"),
    "ggt_daily": _api("官方扩展·行情与交易", 196, min_points=2000, frequency="daily",
                      model_role="southbound_flow", probe_profile="date_range"),
    "bak_daily": _api("官方扩展·行情与交易", 255, min_points=5000, frequency="daily",
                      model_role="daily_cross_section", priority="P1", probe_profile="trade_date"),
    "stk_shock": _api("官方扩展·行情与交易", 451, min_points=6000, frequency="daily",
                      model_role="volatility_event", priority="P1", probe_profile="stock_range"),
    "stk_high_shock": _api("官方扩展·行情与交易", 452, min_points=6000, frequency="daily",
                           model_role="volatility_event", priority="P1", probe_profile="stock_range"),
    "stk_alert": _api("官方扩展·行情与交易", 453, min_points=6000, frequency="daily",
                      model_role="risk_event", priority="P0", probe_profile="stock_range"),
    "ccass_hold": _api("官方扩展·行情与交易", 295, min_points=5000, frequency="daily",
                       model_role="custody_holding", probe_profile="stock_date"),
    "ccass_hold_detail": _api("官方扩展·行情与交易", 274, min_points=8000, frequency="daily",
                              model_role="custody_holding", probe_profile="stock_date"),
    "stk_nineturn": _api("官方扩展·行情与交易", 364, min_points=6000, frequency="daily",
                         model_role="technical_factor", priority="P1", probe_profile="stock_range"),
    "stk_ah_comparison": _api("官方扩展·行情与交易", 399, min_points=5000, frequency="daily",
                              model_role="cross_market_relative_value", probe_profile="trade_date"),
    "slb_len": _api("官方扩展·行情与交易", 331, min_points=2000, frequency="daily",
                    model_role="securities_lending", priority="P1", probe_profile="trade_date"),
    "dc_concept": _api("官方扩展·行情与交易", 421, min_points=6000, frequency="reference_daily",
                       model_role="sector_taxonomy", priority="P0", probe_profile="no_params"),
    "dc_concept_cons": _api("官方扩展·行情与交易", 422, min_points=6000, frequency="reference_daily",
                            model_role="sector_membership", priority="P0", probe_profile="manual"),
    "moneyflow_cnt_ths": _api("官方扩展·行情与交易", 371, min_points=5000, frequency="daily",
                               model_role="sector_flow", priority="P0", probe_profile="trade_date"),

    # ETF reference, constituent and announcement APIs.
    "etf_sh_cons": _api("官方扩展·ETF", 471, min_points=8000, frequency="daily",
                        model_role="etf_constituents", priority="P1", probe_profile="etf_date"),
    "etf_sz_cons": _api("官方扩展·ETF", 472, min_points=8000, frequency="daily",
                        model_role="etf_constituents", priority="P1", probe_profile="etf_date"),
    "idx_anns": _api("官方扩展·ETF", 460, min_points=6000, frequency="event",
                     model_role="index_event", probe_profile="date_range"),

    # Index, industry and market breadth inputs.
    "index_weekly": _api("官方扩展·指数与板块", 171, min_points=600, frequency="weekly",
                         model_role="index_price_history", probe_profile="index_range"),
    "index_monthly": _api("官方扩展·指数与板块", 172, min_points=600, frequency="monthly",
                          model_role="index_price_history", probe_profile="index_range"),
    "index_weight": _api("官方扩展·指数与板块", 96, min_points=2000, frequency="monthly",
                         model_role="benchmark_constituents", priority="P0", probe_profile="index_weight"),
    "index_classify": _api("官方扩展·指数与板块", 181, min_points=2000, frequency="reference_periodic",
                           model_role="sector_taxonomy", priority="P0", probe_profile="index_classify"),
    "index_member_all": _api("官方扩展·指数与板块", 335, min_points=2000, frequency="reference_periodic",
                             model_role="sector_membership", priority="P0", probe_profile="manual"),
    "sw_daily": _api("官方扩展·指数与板块", 327, min_points=5000, frequency="daily",
                     model_role="industry_price_history", priority="P0", probe_profile="sw_range"),
    "ci_index_member": _api("官方扩展·指数与板块", 373, min_points=5000, frequency="reference_periodic",
                            model_role="sector_membership", priority="P1", probe_profile="manual"),
    "ci_daily": _api("官方扩展·指数与板块", 308, min_points=5000, frequency="daily",
                     model_role="industry_price_history", priority="P1", probe_profile="manual"),
    "index_global": _api("官方扩展·指数与板块", 211, min_points=6000, frequency="daily",
                         model_role="global_regime", priority="P1", probe_profile="manual"),
    "idx_factor_pro": _api("官方扩展·指数与板块", 358, min_points=5000, frequency="daily",
                           model_role="index_factor", priority="P1", probe_profile="index_range"),
    "daily_info": _api("官方扩展·指数与板块", 215, min_points=600, frequency="daily",
                       model_role="market_breadth", priority="P0", probe_profile="trade_date"),
    "sz_daily_info": _api("官方扩展·指数与板块", 268, min_points=2000, frequency="daily",
                          model_role="market_breadth", priority="P0", probe_profile="trade_date"),

    # Public fund data useful for allocation, ownership and fund-factor models.
    "fund_company": _api("官方扩展·公募基金", 118, min_points=1500, frequency="reference_periodic",
                         model_role="fund_reference", probe_profile="no_params"),
    "fund_manager": _api("官方扩展·公募基金", 208, min_points=500, frequency="event",
                         model_role="fund_governance", probe_profile="manual"),
    "mkt_idx_bmk": _api("官方扩展·公募基金", 462, min_points=5000, frequency="reference_periodic",
                        model_role="fund_benchmark", probe_profile="fund_range"),
    "fund_share": _api("官方扩展·公募基金", 207, min_points=2000, frequency="daily",
                       model_role="fund_flow", probe_profile="fund_range"),
    "fund_nav": _api("官方扩展·公募基金", 119, min_points=2000, frequency="daily",
                     model_role="fund_nav", probe_profile="fund_range"),
    "fund_div": _api("官方扩展·公募基金", 120, min_points=400, frequency="event",
                     model_role="fund_distribution", probe_profile="fund_range"),
    "fund_portfolio": _api("官方扩展·公募基金", 121, min_points=5000, frequency="quarterly",
                           model_role="institutional_holding", priority="P1", probe_profile="manual"),
    "fund_factor_pro": _api("官方扩展·公募基金", 359, min_points=5000, frequency="daily",
                            model_role="fund_factor", probe_profile="fund_range"),

    # Futures and spot data extend regime, basis, inventory and risk models.
    "fut_basic": _api("官方扩展·期货与现货", 135, min_points=2000, frequency="reference_periodic",
                      model_role="futures_reference", probe_profile="futures_exchange"),
    "fut_trade_cal": _api("官方扩展·期货与现货", 467, min_points=2000, frequency="reference_yearly",
                          model_role="calendar_control", priority="P1", probe_profile="futures_calendar"),
    "fut_daily": _api("官方扩展·期货与现货", 138, min_points=2000, frequency="daily",
                      model_role="futures_price_history", priority="P1", probe_profile="futures_range"),
    "fut_weekly_monthly": _api("官方扩展·期货与现货", 337, min_points=2000, frequency="weekly_monthly",
                               model_role="futures_price_history", probe_profile="futures_range"),
    "fut_wsr": _api("官方扩展·期货与现货", 140, min_points=2000, frequency="weekly",
                    model_role="warehouse_inventory", priority="P1", probe_profile="trade_date"),
    "fut_settle": _api("官方扩展·期货与现货", 141, min_points=2000, frequency="daily",
                       model_role="futures_settlement", probe_profile="trade_date"),
    "fut_holding": _api("官方扩展·期货与现货", 139, min_points=2000, frequency="daily",
                        model_role="futures_positioning", priority="P1", probe_profile="trade_date"),
    "fut_index_daily": _api("官方扩展·期货与现货", 468, min_points=2000, frequency="daily",
                            model_role="commodity_regime", priority="P1", probe_profile="manual"),
    "fut_mapping": _api("官方扩展·期货与现货", 189, min_points=2000, frequency="reference_daily",
                        model_role="futures_mapping", probe_profile="manual"),
    "fut_weekly_detail": _api("官方扩展·期货与现货", 216, min_points=600, frequency="weekly",
                              model_role="futures_positioning", probe_profile="trade_date"),
    "ft_limit": _api("官方扩展·期货与现货", 368, min_points=5000, frequency="daily",
                     model_role="futures_risk_control", priority="P1", probe_profile="trade_date"),
    "sge_basic": _api("官方扩展·期货与现货", 284, min_points=5000, frequency="reference_periodic",
                      model_role="spot_reference", probe_profile="sge_symbol"),
    "sge_daily": _api("官方扩展·期货与现货", 285, min_points=2000, frequency="daily",
                      model_role="precious_metals_regime", priority="P1", probe_profile="date_range"),

    # Convertible bond reference, ownership, valuation and event data.
    "cb_basic": _api("官方扩展·可转债", 185, min_points=2000, frequency="reference_periodic",
                     model_role="convertible_reference", probe_profile="no_params"),
    "cb_issue": _api("官方扩展·可转债", 186, min_points=2000, frequency="event",
                     model_role="convertible_issuance", probe_profile="date_range"),
    "cb_call": _api("官方扩展·可转债", 269, min_points=5000, frequency="event",
                    model_role="convertible_event", priority="P1", probe_profile="date_range"),
    "cb_rate": _api("官方扩展·可转债", 305, min_points=5000, frequency="reference_periodic",
                    model_role="convertible_terms", probe_profile="manual"),
    "top10_cb_holders": _api("官方扩展·可转债", 459, min_points=5000, frequency="quarterly",
                             model_role="convertible_holding", probe_profile="manual"),
    "cb_factor_pro": _api("官方扩展·可转债", 392, min_points=8000, frequency="daily",
                          model_role="convertible_factor", priority="P1", probe_profile="manual"),
    "cb_price_chg": _api("官方扩展·可转债", 246, permission_model="separate_permission", frequency="event",
                         model_role="convertible_event", probe_profile="date_range"),
    "cb_share": _api("官方扩展·可转债", 247, min_points=2000, frequency="event",
                    model_role="convertible_conversion", probe_profile="date_range"),
    "cb_rating": _api("官方扩展·可转债", 458, min_points=2000, frequency="event",
                     model_role="credit_risk", priority="P1", probe_profile="date_range"),

    # Official separately licensed live products. The configured vendors have
    # declared these available only during live sessions; rows are still needed
    # before any one provider becomes verified or decision-eligible.
    "rt_k": _api("实时行情（仅交易时段验证）", 372, permission_model="separate_permission",
                 frequency="realtime", request_policy="market_hours_only", model_role="realtime_quote",
                 priority="P0", required_params=("ts_code",), probe_profile="realtime_stock_k"),
    "rt_min_daily": _api("实时行情（仅交易时段验证）", 457, permission_model="separate_permission",
                         frequency="realtime", request_policy="market_hours_only", model_role="intraday_bar",
                         priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_stock_daily"),
    "rt_etf_k": _api("实时行情（仅交易时段验证）", 400, permission_model="separate_permission",
                     frequency="realtime", request_policy="market_hours_only", model_role="realtime_etf_quote",
                     priority="P0", required_params=("ts_code",), probe_profile="realtime_etf_k"),
    "rt_etf_min_daily": _api("实时行情（仅交易时段验证）", 470, permission_model="separate_permission",
                             frequency="realtime", request_policy="market_hours_only", model_role="intraday_etf_bar",
                             priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_etf_daily"),
    "rt_etf_sz_iopv": _api("实时行情（仅交易时段验证）", 454, permission_model="separate_permission",
                           frequency="realtime", request_policy="market_hours_only", model_role="etf_iopv",
                           priority="P0", required_params=("ts_code",), probe_profile="realtime_etf_iopv"),
    "rt_idx_k": _api("实时行情（仅交易时段验证）", 403, permission_model="separate_permission",
                     frequency="realtime", request_policy="market_hours_only", model_role="realtime_index_quote",
                     priority="P0", required_params=("ts_code",), probe_profile="realtime_index_k"),
    "rt_idx_min_daily": _api("实时行情（仅交易时段验证）", 420, permission_model="separate_permission",
                             frequency="realtime", request_policy="market_hours_only", model_role="intraday_index_bar",
                             priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_index_daily"),
    "rt_sw_k": _api("实时行情（仅交易时段验证）", 417, permission_model="separate_permission",
                    frequency="realtime", request_policy="market_hours_only", model_role="realtime_industry_quote",
                    priority="P0", required_params=("ts_code",), probe_profile="realtime_sw_k"),
    "rt_fut_min_daily": _api("实时行情（仅交易时段验证）", 340, permission_model="separate_permission",
                              frequency="realtime", request_policy="market_hours_only", model_role="intraday_futures_bar",
                              priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_futures_daily"),

    # Historical minute endpoints are inventory entries, not online routes.
    "stk_mins": _api("历史分钟（仅离线文件）", 370, permission_model="offline_delivery", frequency="historical_minute",
                     request_policy="offline_files_only", model_role="intraday_bar", priority="P1"),
    "etf_mins": _api("历史分钟（仅离线文件）", 387, permission_model="offline_delivery", frequency="historical_minute",
                     request_policy="offline_files_only", model_role="intraday_etf_bar", priority="P2"),
    "sw_mins": _api("历史分钟（仅离线文件）", 469, permission_model="offline_delivery", frequency="historical_minute",
                    request_policy="offline_files_only", model_role="intraday_industry_bar", priority="P2"),
    "fut_mins": _api("历史分钟（仅离线文件）", permission_model="offline_delivery", frequency="historical_minute",
                     request_policy="offline_files_only", model_role="intraday_futures_bar", priority="P2"),
    "fut_tick": _api("历史分钟（仅离线文件）", permission_model="offline_delivery", frequency="historical_tick",
                     request_policy="offline_files_only", model_role="futures_tick", priority="P2"),
    "opt_mins": _api("历史分钟（仅离线文件）", permission_model="offline_delivery", frequency="historical_minute",
                     request_policy="offline_files_only", model_role="intraday_option_bar", priority="P2"),
}


# Existing catalog APIs whose official contract needs to override the supplier
# bundle defaults.
OFFICIAL_OVERRIDES: Final[dict[str, OfficialApi]] = {
    "rt_min": _api("实时行情（仅交易时段验证）", 374, permission_model="separate_permission",
                   frequency="realtime", request_policy="market_hours_only", model_role="intraday_bar",
                   priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_stock_min"),
    "rt_etf_min": _api("实时行情（仅交易时段验证）", 416, permission_model="separate_permission",
                       frequency="realtime", request_policy="market_hours_only", model_role="intraday_etf_bar",
                       priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_etf_min"),
    "rt_idx_min": _api("实时行情（仅交易时段验证）", 420, permission_model="separate_permission",
                       frequency="realtime", request_policy="market_hours_only", model_role="intraday_index_bar",
                       priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_index_min"),
    "rt_fut_min": _api("实时行情（仅交易时段验证）", permission_model="provider_contract",
                       frequency="realtime", request_policy="market_hours_only", model_role="intraday_futures_bar",
                       priority="P0", required_params=("ts_code", "freq"), probe_profile="realtime_futures_min"),
    "stk_auction": _api("集合竞价（实测开放）", 369, permission_model="separate_permission",
                        frequency="auction", model_role="auction_microstructure", priority="P0"),
    "stk_auction_o": _api("集合竞价（实测开放）", permission_model="provider_contract",
                          frequency="auction", model_role="auction_microstructure", priority="P0"),
    "stk_auction_c": _api("集合竞价（实测开放）", permission_model="provider_contract",
                          frequency="auction", model_role="auction_microstructure", priority="P0"),
}


# The supplier bundle predates the official-extension metadata above. These
# request shapes are intentionally small permission probes, not sync defaults.
# APIs needing a discovered board, bond, or fund identifier remain manual.
SUPPLIER_PROBE_PROFILES: Final[dict[str, str]] = {
    "trade_cal": "calendar_range", "stock_basic": "stock_only", "etf_basic": "etf_only",
    "etf_index": "etf_only", "opt_basic": "option_exchange", "index_basic": "index_market",
    "namechange": "stock_only", "new_share": "date_range",
    "daily": "stock_range", "weekly": "stock_range", "monthly": "stock_range",
    "adj_factor": "stock_range", "daily_basic": "stock_range", "stk_limit": "stock_range",
    "suspend_d": "stock_range", "fund_daily": "fund_range", "fund_adj": "fund_range",
    "etf_share_size": "etf_date", "index_daily": "index_range", "index_dailybasic": "index_range",
    "cb_daily": "date_range", "hk_hold": "stock_date",
    "income": "stock_range", "income_vip": "stock_range", "balancesheet": "stock_range",
    "balancesheet_vip": "stock_range", "cashflow": "stock_range", "cashflow_vip": "stock_range",
    "forecast": "stock_range", "forecast_vip": "stock_range", "express": "stock_range",
    "express_vip": "stock_range", "dividend": "stock_only", "fina_indicator": "stock_range",
    "fina_indicator_vip": "stock_range", "fina_audit": "stock_range", "fina_mainbz": "stock_only",
    "fina_mainbz_vip": "stock_only", "disclosure_date": "stock_only", "eco_cal": "date_range",
    "shibor": "date_range", "shibor_quote": "date", "shibor_lpr": "date",
    "libor": "date_range", "hibor": "date", "wz_index": "date_range", "gz_index": "date_range",
    "cn_gdp": "gdp_quarter", "cn_cpi": "month", "cn_ppi": "month", "cn_m": "month",
    "sf_month": "month", "cn_pmi": "month", "us_tycr": "date_range", "us_trycr": "date_range",
    "us_tbr": "date_range", "us_tltr": "date_range", "us_trltr": "date_range", "stock_st": "trade_date",
    "stock_hsgt": "hsgt_type", "pledge_stat": "stock_only", "pledge_detail": "stock_only",
    "repurchase": "date_range", "share_float": "stock_only", "block_trade": "stock_range",
    "stk_holdernumber": "stock_only", "stk_holdertrade": "stock_range", "top_list": "trade_date",
    "top_inst": "trade_date", "margin": "trade_date", "margin_detail": "trade_date",
    "margin_secs": "trade_date", "cyq_perf": "stock_range", "cyq_chips": "stock_range",
    "stk_factor": "stock_range", "stk_factor_pro": "stock_range", "report_rc": "stock_range",
    "broker_recommend": "month", "stk_surv": "stock_range", "moneyflow": "stock_range",
    "moneyflow_ths": "stock_range", "moneyflow_dc": "stock_range", "moneyflow_ind_ths": "trade_date",
    "moneyflow_ind_dc": "trade_date", "moneyflow_mkt_dc": "trade_date", "moneyflow_hsgt": "trade_date",
    "limit_list_ths": "trade_date", "limit_list_d": "trade_date", "limit_step": "trade_date",
    "limit_cpt_list": "trade_date", "ths_hot": "trade_date", "dc_hot": "trade_date",
    "hm_list": "no_params", "hm_detail": "trade_date", "ths_index": "ths_index_catalog",
    "ths_daily": "ths_range", "tdx_index": "tdx_only", "tdx_daily": "tdx_range", "kpl_list": "trade_date",
}


AUDIT_FOCUS_APIS: Final[tuple[str, ...]] = (
    "stock_company", "bak_basic", "st", "stk_weekly_monthly", "stk_week_month_adj",
    "stk_shock", "stk_alert", "stk_nineturn", "dc_concept", "index_weight",
    "index_classify", "sw_daily", "daily_info", "sz_daily_info", "fund_company",
    "fund_nav", "fut_basic", "fut_daily", "sge_basic", "cb_basic",
)


def official_spec(api_name: str) -> OfficialApi | None:
    return OFFICIAL_OVERRIDES.get(api_name) or OFFICIAL_EXTENSIONS.get(api_name)


def official_metadata(api_name: str) -> dict[str, Any]:
    spec = official_spec(api_name)
    if spec is None:
        return {
            "catalog_origin": "supplier_109",
            "permission_model": "provider_contract",
            "min_points": None,
            "official_doc_url": None,
            "request_policy": "online_bounded",
            "model_role": "raw_evidence",
            "priority": "P2",
            "required_params": [],
            "probe_profile": SUPPLIER_PROBE_PROFILES.get(api_name, "manual"),
        }
    origin = "official_extension" if api_name in OFFICIAL_EXTENSIONS else "supplier_plus_official"
    return {
        "catalog_origin": origin,
        "permission_model": spec.permission_model,
        "min_points": spec.min_points,
        "official_doc_url": spec.official_doc_url,
        "request_policy": spec.request_policy,
        "model_role": spec.model_role,
        "priority": spec.priority,
        "required_params": list(spec.required_params),
        "probe_profile": spec.probe_profile,
    }


def previous_weekday(value: date) -> date:
    while value.weekday() >= 5:
        value -= timedelta(days=1)
    return value


def default_futures_symbol(value: date) -> str:
    return f"IF{value.year % 100:02d}{value.month:02d}.CFX"


def default_probe_params(api_name: str, *, symbol: str = "000636.SZ", as_of: date | None = None,
                         frequency: str = "1MIN", etf_symbol: str = "159919.SZ",
                         index_symbol: str = "000300.SH", sw_symbol: str = "801080.SI",
                         futures_symbol: str | None = None) -> dict[str, Any] | None:
    """Build a conservative probe request, or return None when parameters need review."""
    spec = official_spec(api_name)
    profile = spec.probe_profile if spec is not None else SUPPLIER_PROBE_PROFILES.get(api_name, "manual")
    if profile == "manual":
        return None
    effective_date = previous_weekday(as_of or date.today())
    end_date = effective_date.strftime("%Y%m%d")
    start_date = (effective_date - timedelta(days=7)).strftime("%Y%m%d")
    future = futures_symbol or default_futures_symbol(effective_date)
    profiles: dict[str, dict[str, Any]] = {
        "no_params": {},
        "company_exchange": {"exchange": "SZSE"},
        "calendar_range": {"exchange": "SSE", "start_date": start_date, "end_date": end_date},
        "etf_only": {"ts_code": etf_symbol},
        "option_exchange": {"exchange": "SSE"},
        "index_market": {"market": "SSE"},
        "trade_date": {"trade_date": end_date},
        "date": {"date": end_date},
        "date_range": {"start_date": start_date, "end_date": end_date},
        "stock_only": {"ts_code": symbol},
        "stock_date": {"ts_code": symbol, "trade_date": end_date},
        "stock_range": {"ts_code": symbol, "start_date": start_date, "end_date": end_date},
        "etf_date": {"ts_code": etf_symbol, "trade_date": end_date},
        "index_range": {"ts_code": index_symbol, "start_date": start_date, "end_date": end_date},
        "index_classify": {"src": "SW2021"},
        "index_weight": {"index_code": index_symbol, "start_date": start_date, "end_date": end_date},
        "sw_range": {"ts_code": sw_symbol, "start_date": start_date, "end_date": end_date},
        "fund_range": {"ts_code": etf_symbol, "start_date": start_date, "end_date": end_date},
        "futures_exchange": {"exchange": "CFFEX", "fut_type": "1"},
        "futures_calendar": {"exchange": "CFFEX", "start_date": start_date, "end_date": end_date},
        "futures_range": {"ts_code": future, "start_date": start_date, "end_date": end_date},
        "sge_symbol": {"ts_code": "Au99.99.SGE"},
        "hsgt_type": {"trade_date": end_date, "type": "HK_SZ"},
        "month": {"month": end_date[:6]},
        "gdp_quarter": {"q": f"{effective_date.year}Q{((effective_date.month - 1) // 3) + 1}"},
        "ths_index_catalog": {"exchange": "A", "type": "N"},
        "ths_range": {"ts_code": "885001.TI", "start_date": start_date, "end_date": end_date},
        "tdx_only": {"ts_code": "880001.TI"},
        "tdx_range": {"ts_code": "880001.TI", "start_date": start_date, "end_date": end_date},
        "realtime_stock_k": {"ts_code": symbol},
        "realtime_stock_min": {"ts_code": symbol, "freq": frequency},
        "realtime_stock_daily": {"ts_code": symbol, "freq": frequency},
        "realtime_etf_k": {"ts_code": etf_symbol},
        "realtime_etf_min": {"ts_code": etf_symbol, "freq": frequency},
        "realtime_etf_daily": {"ts_code": etf_symbol, "freq": frequency},
        "realtime_etf_iopv": {"ts_code": etf_symbol},
        "realtime_index_k": {"ts_code": index_symbol},
        "realtime_index_min": {"ts_code": index_symbol, "freq": frequency},
        "realtime_index_daily": {"ts_code": index_symbol, "freq": frequency},
        "realtime_sw_k": {"ts_code": sw_symbol},
        "realtime_futures_min": {"ts_code": future, "freq": frequency},
        "realtime_futures_daily": {"ts_code": future, "freq": frequency},
    }
    return profiles.get(profile)


def realtime_probe_matrix(*, symbol: str, frequency: str, etf_symbol: str, index_symbol: str,
                          sw_symbol: str, futures_symbol: str | None, as_of: date | None = None) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for api_name in sorted(REALTIME_MARKET_HOURS_APIS):
        params = default_probe_params(api_name, symbol=symbol, as_of=as_of, frequency=frequency,
                                      etf_symbol=etf_symbol, index_symbol=index_symbol,
                                      sw_symbol=sw_symbol, futures_symbol=futures_symbol)
        if params is not None:
            rows.append((api_name, params))
    return rows
