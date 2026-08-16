"""Native-async projections for persisted intraday evidence.

These dashboard reads are deliberately local-only.  Keeping the two list
projections on the small read pool prevents a busy frontend from borrowing a
legacy blocking-executor slot while the intraday scanner is active.  Decision
cards remain on their existing synchronous compatibility path because their
projection still has a larger pure-Python dependency surface.
"""

from __future__ import annotations

from typing import Any


async def watchlists(async_database: Any) -> dict[str, Any]:
    """Return the persisted watchlist without contacting a market provider."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            "SELECT * FROM quant.intraday_watchlists "
            "ORDER BY enabled DESC,available_quantity DESC,updated_at DESC,symbol"
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {"items": rows, "notice": "观察/持仓池用于提醒范围，不会创建或提交任何订单。"}


async def latest_scan(async_database: Any, *, limit: int = 100) -> dict[str, Any]:
    """Return one bounded, already-persisted scan and its outbox evidence."""
    bounded_limit = max(1, min(limit, 200))
    async with async_database.transaction() as connection:
        scan_result = await connection.execute(
            "SELECT * FROM quant.intraday_scan_runs ORDER BY observed_at DESC LIMIT 1"
        )
        scan = await scan_result.fetchone()
        if not scan:
            return {"scan": None, "signals": [], "deliveries": []}
        scan = dict(scan)
        signals_result = await connection.execute(
            "SELECT * FROM quant.intraday_signal_events WHERE scan_id=%s "
            "ORDER BY severity DESC,created_at LIMIT %s",
            (scan["scan_id"], bounded_limit),
        )
        deliveries_result = await connection.execute(
            """SELECT d.* FROM quant.intraday_alert_deliveries d
                 JOIN quant.intraday_signal_events s ON s.signal_event_id=d.signal_event_id
                WHERE s.scan_id=%s ORDER BY d.created_at LIMIT %s""",
            (scan["scan_id"], bounded_limit),
        )
        signals = [dict(row) for row in await signals_result.fetchall()]
        deliveries = [dict(row) for row in await deliveries_result.fetchall()]
    return {"scan": scan, "signals": signals, "deliveries": deliveries}


__all__ = ["latest_scan", "watchlists"]
