"""Pure mapping of order-derived execution facts onto local minute bars."""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal
from typing import Any


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _eligibility(order_at: datetime) -> tuple[datetime, str]:
    clock = order_at.timetz().replace(tzinfo=None)
    if time(9, 15) <= clock < time(9, 30):
        return order_at.replace(hour=9, minute=30, second=0, microsecond=0), "preopen_to_open"
    if time(11, 30) < clock < time(13, 0):
        return order_at.replace(hour=13, minute=0, second=0, microsecond=0), "lunch_to_afternoon"
    if clock > time(15, 0):
        return order_at, "after_close_next_session"
    return order_at, "continuous_auction"


def map_execution_to_bars(execution: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    order_at = execution.get("order_at")
    if not isinstance(order_at, datetime):
        return {"mapping_status": "invalid_order_time", "confidence": "none"}
    eligible_at, session_basis = _eligibility(order_at)
    target = Decimal(str(execution.get("price")))
    candidates = sorted(
        (row for row in bars if isinstance(row.get("bar_time"), datetime) and row["bar_time"] >= eligible_at),
        key=lambda row: row["bar_time"],
    )
    for row in candidates:
        if row.get("low") is None or row.get("high") is None:
            continue
        low, high = Decimal(str(row["low"])), Decimal(str(row["high"]))
        if low <= target <= high:
            delay = int((row["bar_time"] - order_at).total_seconds())
            if session_basis != "continuous_auction":
                confidence = "low"
            elif delay <= 60:
                confidence = "high"
            elif delay <= 900:
                confidence = "medium"
            else:
                confidence = "low"
            return {
                "mapping_status": "inferred_price_cross", "mapped_bar_time": _iso(row["bar_time"]),
                "mapped_bar_close": float(row["close"]), "delay_seconds": delay,
                "confidence": confidence, "session_basis": session_basis,
                "time_basis": "first_local_minute_price_cross_after_order",
            }
    return {
        "mapping_status": "minute_bar_or_price_cross_missing", "mapped_bar_time": None,
        "delay_seconds": None, "confidence": "none", "session_basis": session_basis,
        "time_basis": "order_time_proxy_only",
    }


__all__ = ["map_execution_to_bars"]
