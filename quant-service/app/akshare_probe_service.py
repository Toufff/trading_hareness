"""Bounded, dependency-injected AKShare capability probe orchestration.

The probe is deliberately a research/evidence path.  It performs no strategy
promotion and persists a separate health receipt for each bounded capability.
Keeping the call tree here prevents the API composition module from owning
provider timing, circuit handling and persistence semantics together.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Mapping

from .akshare_provider import AkShareProviderError
from .runtime_executors import ExecutorSaturatedError


async def run(
    payload: Any,
    *,
    today: Callable[[], date],
    run_akshare: Callable[..., Awaitable[list[dict[str, Any]]]],
    run_database: Callable[..., Awaitable[Any]],
    open_provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]],
    persist_result: Callable[..., int],
    persist_failure: Callable[..., None],
    safe_error_detail: Callable[[str, int], str],
    provider_status: Callable[[], dict[str, Any]],
    sources: Mapping[str, Callable[..., list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Probe enabled AKShare capabilities with one bounded source call each."""
    as_of = payload.trade_date or today()
    start = as_of - timedelta(days=payload.lookback_days + 12)
    results: list[dict[str, Any]] = []

    async def run_step(label: str, capability: str, action: Callable[[], list[dict[str, Any]]]) -> None:
        if capability in await open_provider_capabilities("akshare", [capability]):
            results.append({"source": label, "provider": "akshare", "capability": capability,
                            "status": "circuit_open", "received": 0, "stored": 0,
                            "error": "provider health circuit is open; upstream request skipped"})
            return
        started_at = asyncio.get_running_loop().time()
        try:
            rows = await run_akshare(action, timeout_seconds=45)
            latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
            stored = await run_database(
                persist_result, capability, rows, payload.symbol, latency_ms, timeout_seconds=60,
            )
            results.append({"source": label, "provider": "akshare", "capability": capability,
                            "status": "completed" if rows else "empty", "received": len(rows), "stored": stored})
        except ExecutorSaturatedError as error:
            results.append({"source": label, "provider": "akshare", "capability": capability,
                            "status": "blocked", "received": 0, "stored": 0,
                            "error": safe_error_detail(str(error), 300)})
        except (asyncio.TimeoutError, AkShareProviderError, ValueError) as error:
            latency_ms = round((asyncio.get_running_loop().time() - started_at) * 1000)
            await run_database(
                persist_failure, capability, str(error) or "AKShare request failed", latency_ms,
            )
            results.append({"source": label, "provider": "akshare", "capability": capability,
                            "status": "failed", "received": 0, "stored": 0, "error": str(error)[:300]})

    await run_step("AKShare公开日线", "daily_bar", lambda: sources["daily"](
        payload.symbol, start.strftime("%Y%m%d"), as_of.strftime("%Y%m%d"),
    ))
    if payload.include_market_summary:
        await run_step("AKShare上交所市场总貌", "market_summary", sources["market_summary"])
    if payload.include_lhb:
        await run_step("AKShare龙虎榜事件", "lhb_event", lambda: sources["lhb_events"](start, as_of))
    if payload.include_strong_pool:
        await run_step("AKShare强势股池", "strong_pool", lambda: sources["strong_pool"](as_of))
    if payload.include_supplements:
        await run_step("AKShare市场宽度补充", "market_breadth", lambda: sources["market_breadth"](as_of))
        if payload.include_board_taxonomy:
            await run_step("AKShare板块/行业/成分补充", "board_taxonomy", lambda: sources["board_supplements"](payload.board_limit))
        if payload.include_moneyflow:
            await run_step("AKShare资金流补充", "moneyflow_supplement", lambda: sources["moneyflow_supplements"](payload.symbol))
        if payload.include_limit_pools:
            await run_step("AKShare涨跌停情绪池补充", "limit_pool", lambda: sources["limit_pool_events"](as_of))
        if payload.include_lhb_supplements:
            await run_step("AKShare龙虎榜席位统计补充", "lhb_supplement", lambda: sources["lhb_supplements"](start, as_of))
        if payload.include_block_trades:
            await run_step("AKShare大宗交易补充", "block_trade_supplement", lambda: sources["block_trade_supplements"](as_of))
        if payload.include_corporate_risk:
            await run_step("AKShare公司事件风险补充", "corporate_risk_supplement", lambda: sources["corporate_risk_supplements"](payload.symbol, start, as_of))
        if payload.include_analyst_heat:
            await run_step("AKShare分析师/热度/新闻补充", "analyst_heat_supplement", lambda: sources["analyst_heat_supplements"](payload.symbol, as_of.year))
        if payload.include_index_fund:
            await run_step("AKShare指数成分/基金持仓补充", "index_fund_supplement", sources["index_fund_supplements"])
        if payload.include_macro_cross_asset:
            await run_step("AKShare宏观/商品/衍生品补充", "macro_cross_asset_supplement", lambda: sources["macro_cross_asset_supplements"](as_of))
    overall_status = "completed" if any(item["status"] in {"completed", "empty"} for item in results) \
        else "blocked" if results and all(item["status"] in {"circuit_open", "blocked"} for item in results) else "failed"
    return {
        "status": overall_status, "provider": provider_status(), "symbol": payload.symbol,
        "as_of_date": str(as_of), "results": results, "decision_eligible": False,
    }


__all__ = ["run"]
