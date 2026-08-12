"""Bounded read-only views of stored intraday watch, scan, and decision evidence."""

from __future__ import annotations

from typing import Any, Callable


def watchlists(database: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(
            "SELECT * FROM quant.intraday_watchlists ORDER BY enabled DESC,available_quantity DESC,updated_at DESC,symbol"
        ).fetchall()
    return {"items": rows, "notice": "观察/持仓池用于提醒范围，不会创建或提交任何订单。"}


def latest_scan(database: Any, *, limit: int = 100) -> dict[str, Any]:
    """Return a bounded snapshot of one already-persisted scan and its outbox evidence."""
    bounded_limit = max(1, min(limit, 200))
    with database.transaction() as connection:
        scan = connection.execute("SELECT * FROM quant.intraday_scan_runs ORDER BY observed_at DESC LIMIT 1").fetchone()
        if not scan:
            return {"scan": None, "signals": [], "deliveries": []}
        signals = connection.execute(
            "SELECT * FROM quant.intraday_signal_events WHERE scan_id=%s ORDER BY severity DESC,created_at LIMIT %s",
            (scan["scan_id"], bounded_limit),
        ).fetchall()
        deliveries = connection.execute(
            """SELECT d.* FROM quant.intraday_alert_deliveries d JOIN quant.intraday_signal_events s ON s.signal_event_id=d.signal_event_id
               WHERE s.scan_id=%s ORDER BY d.created_at LIMIT %s""",
            (scan["scan_id"], bounded_limit),
        ).fetchall()
    return {"scan": scan, "signals": signals, "deliveries": deliveries}


def decision_card(database: Any, symbol: str, decision_card_fn: Callable[[Any, str], dict[str, Any]]) -> dict[str, Any]:
    with database.transaction() as connection:
        return decision_card_fn(connection, symbol)


__all__ = ["decision_card", "latest_scan", "watchlists"]
