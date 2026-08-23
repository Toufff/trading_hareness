"""Point-in-time board-report context for intraday evidence and replay.

The repository deliberately reads only persisted board reports.  It has no
provider client, scheduler, or current-time lookup, so a replay cannot turn a
missing historical report into a fresh market-data request.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
import math
from typing import Any, Callable


def market_context_from_board_report(
    row: Any,
    observed_at: datetime,
    symbol: str | None = None,
    *,
    strategy_market_state: Callable[[list[dict[str, Any]]], tuple[str, dict[str, Any]]],
    number: Callable[[Any], float | None],
) -> dict[str, Any]:
    """Describe an observation using exactly one already-persisted report."""
    if row is None:
        return {
            "status": "missing", "market_state": "unknown", "board_snapshot_age_seconds": None,
            "symbol_board_matches": [], "notice": "no board snapshot existed before the signal",
        }
    items = list((row["payload"] or {}).get("items") or [])
    market_state, metrics = strategy_market_state(items) if items else ("unknown", {"known_board_flows": 0})
    matches: list[dict[str, Any]] = []
    if symbol:
        for item in items:
            if any(str(stock.get("symbol") or "") == symbol for stock in item.get("top_stocks") or []):
                matches.append({
                    key: item.get(key)
                    for key in ("taxonomy_key", "sector_key", "label", "net_inflow", "change_pct")
                })
    matches.sort(key=lambda item: float(number(item.get("net_inflow")) or -math.inf), reverse=True)
    return {
        "status": "available", "board_report_id": str(row["board_report_id"]),
        "board_observed_at": row["observed_at"].isoformat(),
        "board_snapshot_age_seconds": round(max(0.0, (observed_at - row["observed_at"]).total_seconds()), 1),
        "market_state": market_state, "market_state_metrics": metrics,
        "symbol_board_matches": matches[:8],
        "match_semantics": "saved board Top10 occurrence; not full membership coverage",
    }


def point_in_time_market_context(
    connection: Any,
    observed_at: datetime,
    symbol: str | None = None,
    *,
    context_from_board_report: Callable[[Any, datetime, str | None], dict[str, Any]],
) -> dict[str, Any]:
    """Read only the latest completed board report available at observation time."""
    row = connection.execute(
        """SELECT board_report_id,observed_at,payload FROM quant.intraday_board_reports
             WHERE status='completed' AND observed_at<=%s ORDER BY observed_at DESC LIMIT 1""",
        (observed_at,),
    ).fetchone()
    return context_from_board_report(row, observed_at, symbol)


def point_in_time_market_context_batch(
    connection: Any,
    observations: list[tuple[datetime, str]],
    *,
    context_from_board_report: Callable[[Any, datetime, str | None], dict[str, Any]],
) -> dict[tuple[datetime, str], dict[str, Any]]:
    """Resolve a bounded observation batch with one board-report query."""
    normalized = [(observed_at, str(symbol)) for observed_at, symbol in observations if isinstance(observed_at, datetime)]
    if not normalized:
        return {}
    earliest, latest = min(item[0] for item in normalized), max(item[0] for item in normalized)
    rows = connection.execute(
        """SELECT board_report_id,observed_at,payload FROM quant.intraday_board_reports
             WHERE status='completed' AND observed_at<=%s
               AND (observed_at>=%s OR observed_at=(
                   SELECT max(observed_at) FROM quant.intraday_board_reports
                    WHERE status='completed' AND observed_at<%s
               ))
             ORDER BY observed_at""",
        (latest, earliest, earliest),
    ).fetchall()
    reports = [dict(row) for row in rows]
    report_times = [row["observed_at"] for row in reports]
    contexts: dict[tuple[datetime, str], dict[str, Any]] = {}
    for observed_at, symbol in normalized:
        position = bisect_right(report_times, observed_at) - 1
        report = reports[position] if position >= 0 else None
        contexts[(observed_at, symbol)] = context_from_board_report(report, observed_at, symbol)
    return contexts


__all__ = [
    "market_context_from_board_report",
    "point_in_time_market_context",
    "point_in_time_market_context_batch",
]
