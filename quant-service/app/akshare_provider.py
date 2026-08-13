"""Optional AKShare connector for public-source corroborating evidence."""

from __future__ import annotations

import math
import os
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable


class AkShareProviderError(RuntimeError):
    """Concise public-provider failure without leaking environment details."""


def akshare_enabled() -> bool:
    return os.getenv("AKSHARE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def akshare_status() -> dict[str, str | bool]:
    if not akshare_enabled():
        return {"name": "akshare", "provider_key": "akshare", "label": "AKShare 公开聚合源", "configured": False, "protocol": "python_optional"}
    try:
        import akshare as ak  # type: ignore
    except Exception:
        return {"name": "akshare", "provider_key": "akshare", "label": "AKShare 公开聚合源", "configured": False, "protocol": "python_optional"}
    return {"name": "akshare", "provider_key": "akshare", "label": f"AKShare {ak.__version__}", "configured": True, "protocol": "python_optional"}


def _ak() -> Any:
    if not akshare_enabled():
        raise AkShareProviderError("AKShare connector is disabled")
    try:
        import akshare as ak  # type: ignore
    except Exception as error:
        raise AkShareProviderError("AKShare is not installed") from error
    return ak


def _rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    try:
        cleaned = frame.where(frame.notna(), None)
        return [_json_safe(dict(row)) for row in cleaned.to_dict(orient="records")]
    except Exception as error:
        raise AkShareProviderError("AKShare returned a non-tabular payload") from error


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item") and callable(value.item):
        try:
            return _json_safe(value.item())
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _call(name: str, invoke: Callable[[Any], Any]) -> list[dict[str, Any]]:
    try:
        return _rows(invoke(_ak()))
    except AkShareProviderError:
        raise
    except Exception as error:
        raise AkShareProviderError(f"{name} request failed: {str(error)[:240]}") from error


def _retry_call(name: str, invoke: Callable[[Any], Any], attempts: int = 2) -> list[dict[str, Any]]:
    """Allow one bounded retry for transient public-source failures."""
    error: AkShareProviderError | None = None
    for attempt in range(attempts):
        try:
            return _call(name, invoke)
        except AkShareProviderError as caught:
            error = caught
            if attempt + 1 < attempts:
                time.sleep(0.35 * (attempt + 1))
    assert error is not None
    raise error


def _symbol_code(symbol: str) -> str:
    return symbol.split(".", 1)[0]


def _market_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{exchange.lower()}{code}"


def _symbol_from_code(code: Any) -> str | None:
    text = str(code or "").strip().upper()
    if text.startswith(("SH", "SZ", "BJ")):
        prefix, code_text = text[:2], text[2:]
        return f"{code_text}.{prefix}" if len(code_text) == 6 and code_text.isdigit() else None
    if len(text) != 6 or not text.isdigit():
        return None
    if text.startswith("6"):
        return f"{text}.SH"
    if text.startswith(("0", "3")):
        return f"{text}.SZ"
    if text.startswith(("4", "8", "9")):
        return f"{text}.BJ"
    return None


def _date_yyyymmdd(value: Any) -> str | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def _iso_datetime(value: Any) -> str | None:
    trade_date = _date_yyyymmdd(value)
    if not trade_date:
        return None
    parsed = datetime.strptime(trade_date, "%Y%m%d")
    return parsed.isoformat()


def _market_for(symbol: str) -> str:
    return symbol.rsplit(".", 1)[1].lower()


def _annotated_rows(capability: str, upstream_site: str, ak_function: str, rows: list[dict[str, Any]],
                    limit: int = 200, symbol: str | None = None) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for row in rows[:limit]:
        ts_code = _symbol_from_code(row.get("代码") or row.get("证券代码") or row.get("股票代码") or row.get("成分券代码"))
        annotated.append({
            **({"ts_code": ts_code or symbol} if ts_code or symbol else {}),
            "capability": capability,
            "upstream_site": upstream_site,
            "ak_function": ak_function,
            "raw": row,
            **row,
        })
    return annotated


def _collect_public(specs: list[tuple[str, str, str, Callable[[Any], Any], int]],
                    capability: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for ak_function, upstream_site, record_type, action, limit in specs:
        try:
            # Supplementary post-close sources are deliberately conservative:
            # one retry absorbs a transient disconnect without turning a broad
            # multi-source collection into an aggressive polling loop.
            for row in _annotated_rows(record_type, upstream_site, ak_function, _retry_call(ak_function, action, attempts=2), limit):
                rows.append(row)
        except AkShareProviderError as error:
            errors.append({"ak_function": ak_function, "error": str(error)[:240]})
    if errors:
        rows.append({"capability": capability, "record_type": "partial_error", "upstream_site": "akshare",
                     "ak_function": "collector", "errors": errors})
    return rows


def _pool_events(trade_date: date, specs: list[tuple[str, str, Callable[[Any], Any], str]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    published_at = datetime.combine(trade_date, datetime.min.time()).isoformat()
    for ak_function, event_type, action, url in specs:
        try:
            raw_rows = _retry_call(ak_function, action, attempts=2)
        except AkShareProviderError:
            continue
        for row in raw_rows[:300]:
            symbol = _symbol_from_code(row.get("代码"))
            if not symbol:
                continue
            name = row.get("名称") or symbol
            events.append({
                "ts_code": symbol,
                "event_type": event_type,
                "published_at": published_at,
                "title": f"{event_type}：{name}",
                "url": url,
                "upstream_site": "eastmoney",
                "ak_function": ak_function,
                "raw": row,
            })
    return events


def akshare_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    try:
        raw_rows = _retry_call(
            "stock_zh_a_hist",
            lambda ak: ak.stock_zh_a_hist(symbol=_symbol_code(symbol), period="daily", start_date=start, end_date=end, adjust=""),
            attempts=2,
        )
        upstream_site = "eastmoney"
        ak_function = "stock_zh_a_hist"
        mapping = {"date": "日期", "open": "开盘", "close": "收盘", "high": "最高", "low": "最低", "vol": "成交量", "amount": "成交额", "pct_chg": "涨跌幅", "turnover_rate": "换手率"}
    except AkShareProviderError:
        raw_rows = _retry_call(
            "stock_zh_a_hist_tx",
            lambda ak: ak.stock_zh_a_hist_tx(symbol=_market_symbol(symbol), start_date=start, end_date=end, adjust=""),
            attempts=2,
        )
        upstream_site = "tencent"
        ak_function = "stock_zh_a_hist_tx"
        mapping = {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "vol": "volume", "amount": "amount", "pct_chg": "", "turnover_rate": "turnover"}
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        trade_date = _date_yyyymmdd(row.get(mapping["date"]))
        if not trade_date:
            continue
        rows.append({
            "ts_code": symbol,
            "trade_date": trade_date,
            "open": row.get(mapping["open"]),
            "close": row.get(mapping["close"]),
            "high": row.get(mapping["high"]),
            "low": row.get(mapping["low"]),
            "vol": row.get(mapping["vol"]),
            "amount": row.get(mapping["amount"]),
            "pct_chg": row.get(mapping["pct_chg"]) if mapping["pct_chg"] else None,
            "turnover_rate": row.get(mapping["turnover_rate"]),
            "upstream_site": upstream_site,
            "ak_function": ak_function,
            "raw": row,
        })
    return rows


def akshare_market_summary() -> list[dict[str, Any]]:
    rows = _retry_call("stock_sse_summary", lambda ak: ak.stock_sse_summary(), attempts=2)
    return [{"upstream_site": "sse", "ak_function": "stock_sse_summary", **row} for row in rows]


def akshare_market_breadth(trade_date: date) -> list[dict[str, Any]]:
    stamp = trade_date.strftime("%Y%m%d")
    return _collect_public([
        ("stock_sse_summary", "sse", "sse_market_summary", lambda ak: ak.stock_sse_summary(), 50),
        ("stock_szse_summary", "szse", "szse_market_summary", lambda ak: ak.stock_szse_summary(date=stamp), 50),
        ("stock_a_high_low_statistics", "legulegu", "high_low_statistics", lambda ak: ak.stock_a_high_low_statistics(symbol="all"), 120),
        ("stock_a_below_net_asset_statistics", "legulegu", "below_net_asset_statistics", lambda ak: ak.stock_a_below_net_asset_statistics(symbol="全部A股"), 120),
        ("stock_account_statistics_em", "eastmoney", "account_statistics", lambda ak: ak.stock_account_statistics_em(), 120),
    ], "market_breadth")


def akshare_board_supplements(board_limit: int = 12) -> list[dict[str, Any]]:
    rows = _collect_public([
        ("stock_board_concept_name_em", "eastmoney", "concept_directory_em", lambda ak: ak.stock_board_concept_name_em(), 500),
        ("stock_board_industry_name_em", "eastmoney", "industry_directory_em", lambda ak: ak.stock_board_industry_name_em(), 200),
        ("stock_board_concept_name_ths", "ths", "concept_directory_ths", lambda ak: ak.stock_board_concept_name_ths(), 500),
        ("stock_board_industry_name_ths", "ths", "industry_directory_ths", lambda ak: ak.stock_board_industry_name_ths(), 200),
        ("stock_board_change_em", "eastmoney", "board_change_em", lambda ak: ak.stock_board_change_em(), 200),
    ], "board_taxonomy")
    concept_names = [str(row.get("板块名称") or row.get("名称") or "").strip() for row in rows if row.get("record_type") == "concept_directory_em"]
    industry_names = [str(row.get("板块名称") or row.get("名称") or "").strip() for row in rows if row.get("record_type") == "industry_directory_em"]
    specs: list[tuple[str, str, str, Callable[[Any], Any], int]] = []
    for name in [item for item in concept_names if item][:board_limit]:
        specs.append(("stock_board_concept_cons_em", "eastmoney", f"concept_members:{name}", lambda ak, item=name: ak.stock_board_concept_cons_em(symbol=item), 300))
    for name in [item for item in industry_names if item][:board_limit]:
        specs.append(("stock_board_industry_cons_em", "eastmoney", f"industry_members:{name}", lambda ak, item=name: ak.stock_board_industry_cons_em(symbol=item), 300))
    return rows + _collect_public(specs, "board_members")


def akshare_eastmoney_board_catalog(kind: str) -> list[dict[str, Any]]:
    """Return the exact Eastmoney board directory for live-flow membership joins."""
    if kind == "concept":
        return _retry_call("stock_board_concept_name_em", lambda ak: ak.stock_board_concept_name_em())
    if kind == "industry":
        return _retry_call("stock_board_industry_name_em", lambda ak: ak.stock_board_industry_name_em())
    raise ValueError("kind must be concept or industry")


def akshare_eastmoney_board_members(kind: str, board_name: str) -> list[dict[str, Any]]:
    """Fetch one complete named Eastmoney board constituent response."""
    if kind == "concept":
        return _retry_call("stock_board_concept_cons_em", lambda ak: ak.stock_board_concept_cons_em(symbol=board_name))
    if kind == "industry":
        return _retry_call("stock_board_industry_cons_em", lambda ak: ak.stock_board_industry_cons_em(symbol=board_name))
    raise ValueError("kind must be concept or industry")


def akshare_eastmoney_board_flow(kind: str) -> list[dict[str, Any]]:
    if kind == "concept":
        return _retry_call("stock_fund_flow_concept", lambda ak: ak.stock_fund_flow_concept(symbol="即时"))
    if kind == "industry":
        return _retry_call("stock_fund_flow_industry", lambda ak: ak.stock_fund_flow_industry(symbol="即时"))
    raise ValueError("kind must be concept or industry")


def akshare_tencent_all_a_spot() -> list[dict[str, Any]]:
    return _retry_call("stock_zh_a_spot_tx", lambda ak: ak.stock_zh_a_spot_tx())


def akshare_moneyflow_supplements(symbol: str) -> list[dict[str, Any]]:
    code = _symbol_code(symbol)
    market = _market_for(symbol)
    return _collect_public([
        ("stock_fund_flow_individual", "eastmoney", "stock_fund_flow_realtime", lambda ak: ak.stock_fund_flow_individual(symbol="即时"), 300),
        ("stock_individual_fund_flow", "eastmoney", "single_stock_fund_flow", lambda ak: ak.stock_individual_fund_flow(stock=code, market=market), 120),
        ("stock_individual_fund_flow_rank", "eastmoney", "stock_fund_flow_rank_5d", lambda ak: ak.stock_individual_fund_flow_rank(indicator="5日"), 300),
        ("stock_main_fund_flow", "eastmoney", "main_fund_flow_all", lambda ak: ak.stock_main_fund_flow(symbol="全部股票"), 300),
        ("stock_fund_flow_industry", "eastmoney", "industry_fund_flow_realtime", lambda ak: ak.stock_fund_flow_industry(symbol="即时"), 200),
        ("stock_fund_flow_concept", "eastmoney", "concept_fund_flow_realtime", lambda ak: ak.stock_fund_flow_concept(symbol="即时"), 300),
        ("stock_hsgt_fund_flow_summary_em", "eastmoney", "hsgt_fund_flow_summary", lambda ak: ak.stock_hsgt_fund_flow_summary_em(), 120),
    ], "moneyflow_supplement")


def akshare_limit_pool_events(trade_date: date) -> list[dict[str, Any]]:
    stamp = trade_date.strftime("%Y%m%d")
    return _pool_events(trade_date, [
        ("stock_zt_pool_em", "limit_up_pool", lambda ak: ak.stock_zt_pool_em(date=stamp), "https://quote.eastmoney.com/ztb/detail#type=ztgc"),
        ("stock_zt_pool_previous_em", "previous_limit_pool", lambda ak: ak.stock_zt_pool_previous_em(date=stamp), "https://quote.eastmoney.com/ztb/detail#type=zrzt"),
        ("stock_zt_pool_zbgc_em", "limit_open_pool", lambda ak: ak.stock_zt_pool_zbgc_em(date=stamp), "https://quote.eastmoney.com/ztb/detail#type=zbgc"),
        ("stock_zt_pool_dtgc_em", "limit_down_pool", lambda ak: ak.stock_zt_pool_dtgc_em(date=stamp), "https://quote.eastmoney.com/ztb/detail#type=dtgc"),
        ("stock_zt_pool_sub_new_em", "sub_new_limit_pool", lambda ak: ak.stock_zt_pool_sub_new_em(date=stamp), "https://quote.eastmoney.com/ztb/detail#type=cxgc"),
    ])


def akshare_live_limit_up_pool_events(trade_date: date) -> list[dict[str, Any]]:
    """Fetch only the current limit-up pool for bounded intraday anchoring.

    The full post-close emotion probe deliberately reads five pools.  The
    intraday linkage miner needs only a live, factual limit-up anchor, so it
    must not turn each five-minute board report into five Eastmoney requests.
    """
    stamp = trade_date.strftime("%Y%m%d")
    return _pool_events(trade_date, [
        ("stock_zt_pool_em", "limit_up_pool", lambda ak: ak.stock_zt_pool_em(date=stamp),
         "https://quote.eastmoney.com/ztb/detail#type=ztgc"),
    ])


def akshare_lhb_supplements(start_date: date, end_date: date) -> list[dict[str, Any]]:
    start, end = start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")
    return _collect_public([
        ("stock_lhb_stock_statistic_em", "eastmoney", "lhb_stock_statistic_1m", lambda ak: ak.stock_lhb_stock_statistic_em(symbol="近一月"), 300),
        ("stock_lhb_jgmmtj_em", "eastmoney", "lhb_institution_trade", lambda ak: ak.stock_lhb_jgmmtj_em(start_date=start, end_date=end), 300),
        ("stock_lhb_jgstatistic_em", "eastmoney", "lhb_institution_statistic", lambda ak: ak.stock_lhb_jgstatistic_em(symbol="近一月"), 300),
        ("stock_lhb_yybph_em", "eastmoney", "lhb_broker_seat_rank", lambda ak: ak.stock_lhb_yybph_em(symbol="近一月"), 300),
        ("stock_lhb_traderstatistic_em", "eastmoney", "lhb_trader_statistic", lambda ak: ak.stock_lhb_traderstatistic_em(symbol="近一月"), 300),
    ], "lhb_supplement")


def akshare_block_trade_supplements(trade_date: date) -> list[dict[str, Any]]:
    stamp = trade_date.strftime("%Y%m%d")
    return _collect_public([
        ("stock_dzjy_mrmx", "eastmoney", "block_trade_detail", lambda ak: ak.stock_dzjy_mrmx(symbol="A股", start_date=stamp, end_date=stamp), 300),
        ("stock_dzjy_mrtj", "eastmoney", "block_trade_daily_stat", lambda ak: ak.stock_dzjy_mrtj(start_date=stamp, end_date=stamp), 200),
        ("stock_dzjy_sctj", "eastmoney", "block_trade_market_stat", lambda ak: ak.stock_dzjy_sctj(start_date=stamp, end_date=stamp), 200),
        ("stock_dzjy_yybph", "eastmoney", "block_trade_seat_rank", lambda ak: ak.stock_dzjy_yybph(start_date=stamp, end_date=stamp), 200),
        ("stock_dzjy_hygtj", "eastmoney", "block_trade_industry_stat", lambda ak: ak.stock_dzjy_hygtj(start_date=stamp, end_date=stamp), 200),
    ], "block_trade_supplement")


def akshare_corporate_risk_supplements(symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
    code = _symbol_code(symbol)
    start, end = start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")
    return _collect_public([
        ("stock_cg_lawsuit_cninfo", "cninfo", "corporate_lawsuit", lambda ak: ak.stock_cg_lawsuit_cninfo(symbol="全部", start_date=start, end_date=end), 300),
        ("stock_cg_guarantee_cninfo", "cninfo", "corporate_guarantee", lambda ak: ak.stock_cg_guarantee_cninfo(symbol="全部", start_date=start, end_date=end), 300),
        ("stock_cg_equity_mortgage_cninfo", "cninfo", "equity_mortgage", lambda ak: ak.stock_cg_equity_mortgage_cninfo(date=end), 300),
        ("stock_repurchase_em", "eastmoney", "repurchase", lambda ak: ak.stock_repurchase_em(), 300),
        ("stock_dividend_cninfo", "cninfo", "dividend", lambda ak: ak.stock_dividend_cninfo(symbol=code), 120),
        ("stock_allotment_cninfo", "cninfo", "allotment", lambda ak: ak.stock_allotment_cninfo(symbol=code), 120),
        ("stock_gddh_em", "eastmoney", "shareholder_meeting", lambda ak: ak.stock_gddh_em(), 200),
    ], "corporate_risk_supplement")


def akshare_analyst_heat_supplements(symbol: str, year: int | None = None) -> list[dict[str, Any]]:
    code = _symbol_code(symbol)
    rank_year = str(year or date.today().year)
    return _collect_public([
        ("stock_analyst_rank_em", "eastmoney", "analyst_rank", lambda ak: ak.stock_analyst_rank_em(year=rank_year), 200),
        ("stock_profit_forecast_em", "eastmoney", "profit_forecast", lambda ak: ak.stock_profit_forecast_em(symbol=code), 200),
        ("stock_profit_forecast_ths", "ths", "profit_forecast_ths", lambda ak: ak.stock_profit_forecast_ths(symbol=code), 200),
        ("stock_rank_forecast_cninfo", "cninfo", "forecast_rank_cninfo", lambda ak: ak.stock_rank_forecast_cninfo(date=rank_year), 200),
        ("stock_hot_rank_em", "eastmoney", "hot_rank", lambda ak: ak.stock_hot_rank_em(), 200),
        ("stock_hot_rank_latest_em", "eastmoney", "hot_rank_latest", lambda ak: ak.stock_hot_rank_latest_em(symbol=code), 200),
        ("stock_news_em", "eastmoney", "stock_news", lambda ak: ak.stock_news_em(symbol=code), 120),
        ("stock_comment_em", "eastmoney", "stock_comment", lambda ak: ak.stock_comment_em(), 200),
    ], "analyst_heat_supplement")


def akshare_index_fund_supplements() -> list[dict[str, Any]]:
    current_year = str(date.today().year)
    return _collect_public([
        ("index_csindex_all", "csindex", "csindex_directory", lambda ak: ak.index_csindex_all(), 300),
        ("index_stock_cons", "sina", "index_cons_000300", lambda ak: ak.index_stock_cons(symbol="000300"), 300),
        ("index_stock_cons_csindex", "csindex", "csindex_cons_000300", lambda ak: ak.index_stock_cons_csindex(symbol="000300"), 300),
        ("index_stock_cons_weight_csindex", "csindex", "csindex_weight_000300", lambda ak: ak.index_stock_cons_weight_csindex(symbol="000300"), 300),
        ("index_component_sw", "sws", "sw_component_801001", lambda ak: ak.index_component_sw(symbol="801001"), 300),
        ("fund_portfolio_hold_em", "eastmoney", "fund_portfolio_hold_sample", lambda ak: ak.fund_portfolio_hold_em(symbol="000001", date=current_year), 200),
        ("fund_report_stock_cninfo", "cninfo", "fund_stock_holdings_report", lambda ak: ak.fund_report_stock_cninfo(date=f"{int(current_year)-1}1231"), 300),
    ], "index_fund_supplement")


def akshare_macro_cross_asset_supplements(trade_date: date) -> list[dict[str, Any]]:
    stamp = trade_date.strftime("%Y%m%d")
    return _collect_public([
        ("macro_china_cpi", "stats", "china_cpi", lambda ak: ak.macro_china_cpi(), 120),
        ("macro_china_gdp", "stats", "china_gdp", lambda ak: ak.macro_china_gdp(), 120),
        ("macro_china_money_supply", "stats", "china_money_supply", lambda ak: ak.macro_china_money_supply(), 120),
        ("macro_china_new_financial_credit", "stats", "china_new_financial_credit", lambda ak: ak.macro_china_new_financial_credit(), 120),
        ("bond_zh_us_rate", "sina", "china_us_rate", lambda ak: ak.bond_zh_us_rate(), 120),
        ("rate_interbank", "chinamoney", "interbank_rate", lambda ak: ak.rate_interbank(market="上海银行同业拆借市场", symbol="Shibor人民币", indicator="隔夜"), 120),
        ("futures_zh_spot", "sina", "futures_spot_sample", lambda ak: ak.futures_zh_spot(symbol="CU0", market="CF", adjust="0"), 20),
        ("option_risk_indicator_sse", "sse", "option_risk_indicator", lambda ak: ak.option_risk_indicator_sse(date=stamp), 120),
        ("energy_oil_hist", "energy", "oil_hist", lambda ak: ak.energy_oil_hist(), 120),
    ], "macro_cross_asset_supplement")


def akshare_lhb_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    raw_rows = _retry_call(
        "stock_lhb_detail_em",
        lambda ak: ak.stock_lhb_detail_em(start_date=start_date.strftime("%Y%m%d"), end_date=end_date.strftime("%Y%m%d")),
        attempts=2,
    )
    events: list[dict[str, Any]] = []
    for row in raw_rows:
        symbol = _symbol_from_code(row.get("代码"))
        occurred_at = _iso_datetime(row.get("上榜日"))
        if not symbol or not occurred_at:
            continue
        reason = str(row.get("上榜原因") or row.get("解读") or "龙虎榜").strip()
        title = f"龙虎榜：{row.get('名称') or symbol} {reason}"
        events.append({
            "ts_code": symbol,
            "event_type": "lhb_event",
            "published_at": occurred_at,
            "title": title,
            "url": "https://data.eastmoney.com/stock/tradedetail.html",
            "upstream_site": "eastmoney",
            "ak_function": "stock_lhb_detail_em",
            "raw": row,
        })
    return events


def akshare_strong_pool_events(trade_date: date) -> list[dict[str, Any]]:
    raw_rows = _retry_call("stock_zt_pool_strong_em", lambda ak: ak.stock_zt_pool_strong_em(date=trade_date.strftime("%Y%m%d")), attempts=2)
    events: list[dict[str, Any]] = []
    published_at = datetime.combine(trade_date, datetime.min.time()).isoformat()
    for row in raw_rows:
        symbol = _symbol_from_code(row.get("代码"))
        if not symbol:
            continue
        title = f"强势股池：{row.get('名称') or symbol}"
        events.append({
            "ts_code": symbol,
            "event_type": "strong_pool",
            "published_at": published_at,
            "title": title,
            "url": "https://quote.eastmoney.com/ztb/detail#type=qsgc",
            "upstream_site": "eastmoney",
            "ak_function": "stock_zt_pool_strong_em",
            "raw": row,
        })
    return events
