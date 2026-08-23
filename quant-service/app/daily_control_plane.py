"""Read-only daily control-plane readiness policy.

Daily adjustment factors and price limits are equities-only controls.  Index
rows may coexist with the full-market daily table, but they intentionally do
not have ``adj_factor`` or ``stk_limit`` records and must not make the equity
decision gate appear unhealthy.
"""

from __future__ import annotations

from typing import Any, Mapping


EQUITY_DAILY_CONTROL_STATUS_SQL = """SELECT bar.trading_date,count(*)::int AS daily_rows,
       count(*) FILTER (WHERE bar.adj_factor IS NOT NULL)::int AS adjustment_rows,
       count(*) FILTER (WHERE bar.limit_up IS NOT NULL AND bar.limit_down IS NOT NULL)::int AS limit_rows
  FROM quant.canonical_bars_daily bar
  JOIN quant.instruments instrument ON instrument.symbol=bar.symbol
 WHERE bar.trading_date=(SELECT max(trading_date) FROM quant.canonical_bars_daily)
   AND instrument.list_date IS NOT NULL
 GROUP BY bar.trading_date"""


def status_payload(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return an explicit fail-closed readiness result from one aggregate row."""
    if not row:
        return {"state": "absent", "reason": "no canonical equity daily bars"}
    daily_rows = int(row["daily_rows"])
    adjustment_rows = int(row["adjustment_rows"])
    limit_rows = int(row["limit_rows"])
    ready = daily_rows > 0 and adjustment_rows == daily_rows and limit_rows == daily_rows
    return {
        "state": "ready" if ready else "blocked",
        "trade_date": str(row["trading_date"]),
        "daily_rows": daily_rows,
        "adjustment_rows": adjustment_rows,
        "limit_rows": limit_rows,
        "reason": None if ready else "latest canonical equity daily bars are missing same-date adjustment or limit controls",
    }


__all__ = ["EQUITY_DAILY_CONTROL_STATUS_SQL", "status_payload"]
