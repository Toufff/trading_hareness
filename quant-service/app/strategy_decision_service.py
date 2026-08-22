"""Evidence-only strategy decision orchestration.

This module owns the decision snapshot call chain, but not the application's
provider clients or database singleton.  The composition root injects those
dependencies, which keeps the route compatible while making the workflow
readable and independently testable by maintenance agents.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .request_models import IntradaySectorReportRequest


async def run(
    request: Any,
    *,
    db: Any,
    run_database_blocking: Callable[..., Awaitable[Any]],
    build_intraday_report: Callable[..., Awaitable[dict[str, Any]]],
    market_regime: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    select_candidates: Callable[[list[dict[str, Any]], int], list[dict[str, Any]]],
    event_context: Callable[[list[str], datetime], dict[str, list[dict[str, Any]]]],
    tushare_lhb_context: Callable[[list[str], datetime], dict[str, list[dict[str, Any]]]],
    source_readiness: Callable[[datetime], dict[str, Any]],
    tushare_realtime_validation: Callable[[list[str], bool], Awaitable[dict[str, Any]]],
    exchange_for: Callable[[str], str],
    json_safe: Callable[[Any], Any],
    model_version: str,
) -> dict[str, Any]:
    """Persist a reproducible, non-executable intraday/close decision snapshot."""
    observed_at = datetime.now(timezone.utc)
    report = await build_intraday_report(
        IntradaySectorReportRequest(kind=request.kind, top_stocks=10, hydrate_top_boards=3)
    )
    china_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    run_id = uuid.uuid4()
    if report.get("status") != "completed":
        def persist_blocked() -> None:
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO quant.recommendation_runs(run_id,as_of_date,model_version,market_regime,source_status,status,model_metadata)
                       VALUES(%s,%s,%s,'blocked',%s,'blocked',%s)""",
                    (run_id, china_date, model_version,
                     Json(json_safe({"intraday_report": report.get("sources", {}), "reason": report.get("reason")})),
                     Json({"session": request.session, "decision_eligible": False})),
                )
        await run_database_blocking(persist_blocked)
        return {
            "status": "blocked", "run_id": str(run_id), "observed_at": observed_at.isoformat(),
            "decision_eligible": False, "reason": report.get("reason", "intraday source unavailable"),
        }

    regime, regime_metrics = market_regime(report["items"])
    candidates = select_candidates(report["items"], request.limit)
    symbols = [candidate["symbol"] for candidate in candidates]
    events, tushare_lhb, readiness = await asyncio.gather(
        run_database_blocking(event_context, symbols, observed_at),
        run_database_blocking(tushare_lhb_context, symbols, observed_at),
        run_database_blocking(source_readiness, observed_at),
    )
    realtime = await tushare_realtime_validation(symbols, request.validate_tushare_realtime)
    coverage = report.get("coverage", {})
    mapped_boards = sum(int(item.get("boards_with_members") or 0) for item in coverage.values())
    flow_boards = sum(int(item.get("flow_boards") or 0) for item in coverage.values())
    coverage_complete = flow_boards > 0 and mapped_boards >= flow_boards
    source_status = {
        "eastmoney_board_flow": "completed", "tencent_quote": "completed",
        "tushare_close_context": report.get("tushare_context", {}),
        "tushare_realtime_validation": realtime,
        "mapping": {"mapped_boards": mapped_boards, "flow_boards": flow_boards, "complete": coverage_complete},
        "akshare_and_cninfo_event_context": "next_session_context_only",
        "tushare_lhb_context": "next_session_context_only",
        "source_readiness": readiness, "decision_eligible": False,
    }

    def persist_completed() -> None:
        with db.transaction() as connection:
            connection.execute(
                """INSERT INTO quant.recommendation_runs(run_id,as_of_date,model_version,market_regime,source_status,status,model_metadata)
                   VALUES(%s,%s,%s,%s,%s,'completed',%s)""",
                (run_id, china_date, model_version, regime, Json(json_safe(source_status)),
                 Json({"session": request.session, "observed_at": observed_at.isoformat(),
                       "regime_metrics": regime_metrics, "decision_eligible": False,
                       "notice": "研究候选池，不构成自动交易指令"})),
            )
            for rank, candidate in enumerate(candidates, start=1):
                flags = list(candidate["risk_flags"])
                if not coverage_complete:
                    flags.append("incomplete_board_mapping")
                event_context_rows = events.get(candidate["symbol"], [])
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange,name,source) VALUES(%s,%s,%s,'strategy_decision') ON CONFLICT(symbol) DO NOTHING",
                    (candidate["symbol"], exchange_for(candidate["symbol"]), candidate.get("name")),
                )
                connection.execute(
                    """INSERT INTO quant.recommendations(run_id,rank,symbol,decision,score,score_breakdown,explanation,risk_flags,
                          direction,horizon_days,confidence,valid_until,invalidation)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)""",
                    (run_id, rank, candidate["symbol"], candidate["decision"], candidate["score"],
                     Json({key: candidate[key] for key in (
                         "board_score", "board_net_inflow", "board_change_pct", "main_net_inflow",
                         "volume_ratio", "turnover_rate", "pct_change",
                     )}),
                     Json({"sector": {key: candidate[key] for key in ("taxonomy_key", "sector_key", "sector_label")},
                           "post_close_context": json_safe(event_context_rows[:5]),
                           "tushare_lhb_context": json_safe(tushare_lhb.get(candidate["symbol"], [])[:5]),
                           "notice": "龙虎榜和涨停池仅作下一交易日背景，不参与盘中打分"}),
                     Json(sorted(set(flags))), 1 if candidate["decision"] == "research_candidate" else 0,
                     candidate["confidence"], china_date,
                     Json(["data_stale", "board_flow_reverses", "main_net_inflow_reverses", "price_extension"])),
                )

    await run_database_blocking(persist_completed, timeout_seconds=60)
    return {
        "status": "completed", "run_id": str(run_id), "observed_at": observed_at.isoformat(),
        "as_of_date": str(china_date), "market_regime": regime, "regime_metrics": regime_metrics,
        "decision_eligible": False, "coverage": source_status["mapping"],
        "tushare_realtime_validation": realtime, "recommendations": candidates,
        "notice": "研究候选池，不构成自动交易指令；龙虎榜仅作为下一交易日背景。",
    }


__all__ = ["run"]
