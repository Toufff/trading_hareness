"""Native-async projection of persisted intraday outcomes.

The dashboard can refresh this bounded result during an active scan.  Keeping
the read path on the native psycopg pool avoids consuming a legacy blocking
database-executor slot; all attribution remains a pure projection of stored
evidence and never contacts a market provider.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from typing import Any, Callable


async def latest_intraday_outcomes(
    async_database: Any,
    limit: int,
    *,
    market_context_from_board_report_fn: Callable[[Any, datetime, str | None], dict[str, Any]],
    attribution_fn: Callable[[str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]], dict[str, Any]],
    attribution_summary_fn: Callable[[list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    """Read a bounded outcome window and batch its point-in-time board context."""
    bounded_limit = max(1, min(limit, 500))
    attribution_window_limit = 5000
    missing_context = {
        "status": "missing", "market_state": "unknown", "board_snapshot_age_seconds": None,
        "symbol_board_matches": [], "notice": "no board snapshot existed before the signal",
    }
    async with async_database.transaction() as connection:
        raw_result = await connection.execute(
            """SELECT o.signal_event_id,o.horizon_key,o.direction,o.entry_observed_at,o.entry_price,o.exit_observed_at,o.exit_price,
                      o.raw_return,o.maximum_favorable_excursion,o.maximum_adverse_excursion,o.status,o.tradability,o.source_status,o.calculated_at,
                      s.symbol,s.signal_key,s.signal_type,s.severity,s.state,s.score,s.observed_at,s.conditions,s.evidence,s.risk_flags
                 FROM quant.intraday_signal_outcomes o
                 JOIN quant.intraday_signal_events s ON s.signal_event_id=o.signal_event_id
                ORDER BY o.calculated_at DESC,s.observed_at DESC,o.horizon_key
                LIMIT %s""",
            (attribution_window_limit,),
        )
        raw_items = [dict(row) for row in await raw_result.fetchall()]
        observations = [
            (item["observed_at"], str(item["symbol"]))
            for item in raw_items if isinstance(item.get("observed_at"), datetime)
        ]
        contexts: dict[tuple[datetime, str], dict[str, Any]] = {}
        if observations:
            earliest, latest = min(item[0] for item in observations), max(item[0] for item in observations)
            report_result = await connection.execute(
                """SELECT board_report_id,observed_at,payload FROM quant.intraday_board_reports
                     WHERE status='completed' AND observed_at<=%s
                       AND (observed_at>=%s OR observed_at=(
                           SELECT max(observed_at) FROM quant.intraday_board_reports
                            WHERE status='completed' AND observed_at<%s
                       ))
                     ORDER BY observed_at""",
                (latest, earliest, earliest),
            )
            reports = [dict(row) for row in await report_result.fetchall()]
            report_times = [row["observed_at"] for row in reports]
            for observed_at, symbol in observations:
                position = bisect_right(report_times, observed_at) - 1
                report = reports[position] if position >= 0 else None
                contexts[(observed_at, symbol)] = market_context_from_board_report_fn(report, observed_at, symbol)
        summary_result = await connection.execute(
            """SELECT horizon_key,status,count(*)::int rows,avg(raw_return) avg_directional_return,
                      avg(maximum_favorable_excursion) avg_mfe,avg(maximum_adverse_excursion) avg_mae
                 FROM quant.intraday_signal_outcomes
                 GROUP BY horizon_key,status ORDER BY horizon_key,status""",
        )
        summary = [dict(row) for row in await summary_result.fetchall()]
    rows: list[dict[str, Any]] = []
    attribution_cache: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        event_key = str(item["signal_event_id"])
        attribution = attribution_cache.get(event_key)
        if attribution is None:
            attribution = attribution_fn(
                str(item["signal_key"]), str(item["signal_type"]), item.get("conditions"), evidence,
                contexts.get((item["observed_at"], str(item["symbol"])), missing_context),
            )
            attribution_cache[event_key] = attribution
        item["attribution"] = attribution
        item.pop("evidence", None)
        rows.append(item)
    attribution_summary = attribution_summary_fn(rows)
    return {
        "items": rows[:bounded_limit], "summary": summary, "attribution_summary": attribution_summary["items"],
        "attribution_validation_gate": attribution_summary["validation_gate"],
        "attribution_window_outcomes": len(rows), "attribution_window_limit": attribution_window_limit,
        "notice": "结果只衡量信号后的可观察价格路径，不代表成交、收益承诺或自动交易表现。",
    }


__all__ = ["latest_intraday_outcomes"]
