"""Bounded multi-source assembly for the single-stock research endpoint.

The service composes independently bounded source probes.  It deliberately
keeps all results labelled by source and never promotes research evidence into
an executable strategy decision.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class StockStudyDependencies:
    china_today: Callable[[], date]
    tushare_request: Callable[..., Any]
    daily_sync_request: Callable[..., Any]
    fetch_tushare: Callable[[str, Any], Awaitable[tuple[dict[str, Any], list[dict[str, Any]]]]]
    realtime_market_session: Callable[[], Awaitable[tuple[bool, str]]]
    sync_baostock: Callable[[Any], Awaitable[dict[str, Any]]]
    free_fetch: Callable[[str, str, str, Callable[[], Awaitable[Any]], str], Awaitable[tuple[dict[str, Any], Any]]]
    eastmoney_daily: Callable[[str, str, str], Awaitable[list[dict[str, Any]]]]
    eastmoney_quote: Callable[[str], Awaitable[dict[str, Any] | None]]
    run_akshare: Callable[..., Awaitable[Any]]
    akshare_daily: Callable[..., Any]
    tencent_daily: Callable[[str, str, str], Awaitable[list[dict[str, Any]]]]
    sina_quote: Callable[[str], Awaitable[dict[str, Any] | None]]
    cninfo_announcements: Callable[..., Awaitable[list[dict[str, Any]]]]
    run_database: Callable[..., Awaitable[Any]]
    persist_market_events: Callable[..., int]
    persist_announcement_health: Callable[..., None]
    technical_summary: Callable[[list[dict[str, Any]]], dict[str, Any]]
    analyst_claims: Callable[[str], tuple[list[dict[str, Any]], dict[str, Any]]]
    recent_events: Callable[[str, int], list[dict[str, Any]]]
    window_readiness: Callable[[str, date, date], dict[str, Any]]
    latest_row: Callable[[list[dict[str, Any]]], dict[str, Any] | None]


def _market_date(as_of: date) -> date:
    if as_of.weekday() == 6:
        return as_of - timedelta(days=2)
    if as_of.weekday() == 5:
        return as_of - timedelta(days=1)
    return as_of


def _tushare_fetches(symbol: str, start: str, end: str, request: Any) -> list[tuple[str, Any]]:
    dated = {"ts_code": symbol, "start_date": start, "end_date": end}
    return [
        ("主 Tushare 日线", request(api_name="daily", provider="primary", params=dated, fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount", max_rows=60)),
        ("超级源日线", request(api_name="daily", provider="super", params=dated, fields="ts_code,trade_date,open,high,low,close,pre_close,vol,amount", max_rows=60)),
        ("REST 备用基础信息", request(api_name="stock_basic", provider="backup", params={"ts_code": symbol, "limit": 3}, max_rows=3)),
        ("复权因子", request(api_name="adj_factor", params=dated, max_rows=60)),
        ("每日估值指标", request(api_name="daily_basic", params=dated, max_rows=60)),
        ("涨跌停价格", request(api_name="stk_limit", params=dated, max_rows=60)),
        ("个股资金流", request(api_name="moneyflow", params=dated, max_rows=60)),
        ("同花顺个股资金流", request(api_name="moneyflow_ths", params=dated, max_rows=60)),
        ("东财个股资金流", request(api_name="moneyflow_dc", params=dated, max_rows=60)),
        ("筹码及胜率", request(api_name="cyq_perf", params=dated, max_rows=60)),
        ("筹码分布", request(api_name="cyq_chips", params=dated, max_rows=500)),
        ("技术因子专业版", request(api_name="stk_factor_pro", params=dated, max_rows=60)),
    ]


async def build(symbol: str, request: Any, deps: StockStudyDependencies) -> dict[str, Any]:
    """Collect the existing bounded evidence set for a single stock study."""
    as_of = request.as_of_date or deps.china_today()
    market_date = _market_date(as_of)
    calendar_span = min(45, max(request.lookback_days + 12, 32))
    start_date = market_date - timedelta(days=calendar_span)
    start, end = start_date.strftime("%Y%m%d"), market_date.strftime("%Y%m%d")
    fetches = _tushare_fetches(symbol, start, end, deps.tushare_request)
    realtime_active, realtime_reason = await deps.realtime_market_session()
    if realtime_active:
        fetches.extend([
            ("主源实时分钟", deps.tushare_request(api_name="rt_min", provider="primary", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=3)),
            ("超级源实时分钟", deps.tushare_request(api_name="rt_min", provider="super", params={"ts_code": symbol, "freq": "1MIN"}, max_rows=3)),
        ])

    baostock_task = asyncio.create_task(deps.sync_baostock(deps.daily_sync_request(trade_date=market_date, symbols=[symbol])))
    results = await asyncio.gather(*(deps.fetch_tushare(label, payload) for label, payload in fetches))
    free_results = await asyncio.gather(
        deps.free_fetch("东方财富公开日线", "eastmoney_free", "daily_bar", lambda: deps.eastmoney_daily(symbol, start, end), symbol),
        deps.free_fetch("东方财富公开报价", "eastmoney_free", "realtime_quote", lambda: deps.eastmoney_quote(symbol), symbol),
        deps.free_fetch("AKShare公开日线", "akshare", "daily_bar", lambda: deps.run_akshare(deps.akshare_daily, symbol, start, end, timeout_seconds=12), symbol),
        deps.free_fetch("腾讯财经公开日线", "tencent_free", "daily_bar", lambda: deps.tencent_daily(symbol, start, end), symbol),
        deps.free_fetch("新浪财经公开报价", "sina_free", "realtime_quote", lambda: deps.sina_quote(symbol), symbol),
    )
    sources = [result[0] for result in results]
    if not realtime_active:
        sources.extend([
            {"source": "主源实时分钟", "api_name": "rt_min", "provider": "primary", "status": "skipped", "received": 0, "stored": 0, "error": realtime_reason},
            {"source": "超级源实时分钟", "api_name": "rt_min", "provider": "super", "status": "skipped", "received": 0, "stored": 0, "error": realtime_reason},
        ])
    sources.extend(result[0] for result in free_results)
    data = {label: rows for (label, _), (_, rows) in zip(fetches, results, strict=True)}
    free_data = {result[0]["source"]: result[1] for result in free_results}
    try:
        baostock = await asyncio.wait_for(baostock_task, timeout=15)
    except asyncio.TimeoutError:
        baostock = {"status": "failed", "imported": 0, "failures": ["study source exceeded 15 second budget"]}
    sources.append({"source": "Baostock 日线", "api_name": "daily_bar", "provider": "baostock", "status": baostock["status"],
                    "received": baostock.get("imported", 0), "stored": baostock.get("imported", 0), "failures": baostock.get("failures", [])})

    announcement_started_at = asyncio.get_running_loop().time()
    try:
        announcement_rows = await asyncio.wait_for(
            deps.cninfo_announcements(symbol, start_date, market_date, max_pages=1), timeout=12,
        )
        announcement_stored = await deps.run_database(deps.persist_market_events, "cninfo_free", announcement_rows, timeout_seconds=60)
        await deps.run_database(
            deps.persist_announcement_health, "completed", announcement_stored, [],
            round((asyncio.get_running_loop().time() - announcement_started_at) * 1000),
        )
        sources.append({"source": "巨潮公开公告", "api_name": "announcement", "provider": "cninfo_free",
                        "status": "completed" if announcement_rows else "empty", "received": len(announcement_rows), "stored": announcement_stored})
    except Exception as error:  # noqa: BLE001 - bounded study reports source failure as evidence
        announcement_rows = []
        await deps.run_database(
            deps.persist_announcement_health, "failed", 0, [str(error)],
            round((asyncio.get_running_loop().time() - announcement_started_at) * 1000),
        )
        sources.append({"source": "巨潮公开公告", "api_name": "announcement", "provider": "cninfo_free",
                        "status": "failed", "received": 0, "stored": 0, "error": str(error)[:300]})

    daily_rows = data["主 Tushare 日线"] or data["超级源日线"]
    technical = deps.technical_summary(daily_rows)
    claims, analyst = await deps.run_database(deps.analyst_claims, symbol)
    announcements = await deps.run_database(deps.recent_events, symbol, 20)
    technical_component = ((technical["score"] - 50) / 50) if technical.get("score") is not None else 0.0
    combined_score = round(max(0, min(100, 50 + technical_component * 25 + analyst["score"] * 25)), 1)
    stance = "research_positive" if combined_score >= 62 else "research_negative" if combined_score <= 38 else "mixed_or_insufficient"
    profile = deps.latest_row(data["REST 备用基础信息"])
    readiness = await deps.run_database(deps.window_readiness, symbol, start_date, market_date)
    return {
        "symbol": symbol, "as_of_date": str(market_date), "lookback_days": request.lookback_days, "sources": sources,
        "on_demand_readiness": readiness,
        "market": {
            "daily_bars": daily_rows[-45:], "latest_realtime": deps.latest_row(data.get("主源实时分钟", []) or data.get("超级源实时分钟", [])),
            "eastmoney_quote": free_data["东方财富公开报价"], "eastmoney_daily_bars": free_data["东方财富公开日线"],
            "akshare_daily_bars": free_data["AKShare公开日线"], "tencent_daily_bars": free_data["腾讯财经公开日线"],
            "sina_quote": free_data["新浪财经公开报价"], "latest_adj_factor": deps.latest_row(data["复权因子"]),
            "latest_limit": deps.latest_row(data["涨跌停价格"]), "latest_daily_basic": deps.latest_row(data["每日估值指标"]),
            "latest_moneyflow": deps.latest_row(data["个股资金流"]), "latest_ths_moneyflow": deps.latest_row(data["同花顺个股资金流"]),
            "latest_dc_moneyflow": deps.latest_row(data["东财个股资金流"]), "latest_chip": deps.latest_row(data["筹码及胜率"]),
            "latest_chip_distribution": deps.latest_row(data["筹码分布"]), "latest_factor": deps.latest_row(data["技术因子专业版"]),
            "profile": profile,
        },
        "events": {"announcements": announcements, "provider": "cninfo_free", "decision_eligible": False},
        "technical": technical, "analyst": {"summary": analyst, "claims": claims},
        "combined": {
            "score": combined_score, "stance": stance,
            "notice": "研究结论基于当前可得数据与远端分析师证据，不构成交易指令。",
            "reasons": [*technical.get("reasons", [])[:3], f"远端分析师有效观点 {analyst['claim_count']} 条，聚合方向为 {analyst['direction']}"],
        },
    }


__all__ = ["StockStudyDependencies", "build"]
