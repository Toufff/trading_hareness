"""Pure timestamp validation for on-demand and intraday study data."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .tushare_official import REALTIME_MARKET_HOURS_APIS


def looks_like_response_header(rows: list[dict[str, Any]]) -> bool:
    """Reject a path gateway's repeated field-name rows as non-market data."""
    return bool(rows) and all(bool(row) and all(str(value) == key for key, value in row.items()) for row in rows)


def _row_trade_datetime(row: dict[str, Any]) -> datetime | None:
    china = ZoneInfo("Asia/Shanghai")
    for key in ("trade_time", "updated_at", "time", "trade_date", "date", "datetime"):
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:19], fmt).replace(tzinfo=china)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=china) if parsed.tzinfo is None else parsed.astimezone(china)
        except ValueError:
            continue
    return None


def _row_trade_date(row: dict[str, Any]) -> date | None:
    stamped_at = _row_trade_datetime(row)
    return stamped_at.date() if stamped_at else None


def realtime_rows_are_current(api_name: str, rows: list[dict[str, Any]], as_of: date | None = None,
                              observed_at: datetime | None = None) -> bool:
    """Require same-day timestamps for live data, except verified un-stamped snapshots."""
    if api_name not in REALTIME_MARKET_HOURS_APIS or not rows:
        return True
    china = ZoneInfo("Asia/Shanghai")
    current = (observed_at or datetime.now(timezone.utc)).astimezone(china)
    expected = as_of or current.date()
    if api_name in {"rt_k", "rt_etf_k"} and all(
        not any(row.get(key) for key in ("trade_time", "updated_at", "time", "trade_date", "date", "datetime"))
        for row in rows
    ):
        return True
    timestamps = [stamped_at for row in rows if (stamped_at := _row_trade_datetime(row)) is not None]
    if (api_name.endswith("_min") or api_name.endswith("_min_daily")) and expected == current.date():
        if any(stamped_at > current + timedelta(minutes=2) for stamped_at in timestamps):
            return False
    return any(_row_trade_date(row) == expected for row in rows)
