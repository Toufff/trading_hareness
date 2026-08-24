"""Read-only daily control-plane readiness policy.

Daily adjustment factors and price limits are equities-only controls.  Index
rows may coexist with the full-market daily table, but they intentionally do
not have ``adj_factor`` or ``stk_limit`` records and must not make the equity
decision gate appear unhealthy.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


MINIMUM_ALL_A_COVERAGE_RATIO = 0.95


EQUITY_DAILY_CONTROL_STATUS_SQL = """WITH latest AS (
       SELECT max(trading_date) AS trading_date FROM quant.canonical_bars_daily
   ), expected AS (
       SELECT latest.trading_date,count(DISTINCT membership.symbol)::int AS expected_daily_rows
         FROM latest
         LEFT JOIN quant.universe_membership_history membership
           ON membership.universe_key='all_a'
          AND membership.effective_from<=latest.trading_date
          AND (membership.effective_to IS NULL OR membership.effective_to>=latest.trading_date)
        GROUP BY latest.trading_date
   ) SELECT expected.trading_date,expected.expected_daily_rows,
       count(DISTINCT bar.symbol)::int AS daily_rows,
       count(DISTINCT bar.symbol) FILTER (WHERE bar.adj_factor IS NOT NULL)::int AS adjustment_rows,
       count(DISTINCT bar.symbol) FILTER (WHERE bar.limit_up IS NOT NULL AND bar.limit_down IS NOT NULL)::int AS limit_rows
     FROM expected
     LEFT JOIN quant.canonical_bars_daily bar
       ON bar.trading_date=expected.trading_date
      AND bar.quality_status IN ('fresh','partial')
     LEFT JOIN quant.universe_membership_history membership
       ON membership.universe_key='all_a' AND membership.symbol=bar.symbol
      AND membership.effective_from<=expected.trading_date
      AND (membership.effective_to IS NULL OR membership.effective_to>=expected.trading_date)
    WHERE membership.symbol IS NOT NULL OR bar.symbol IS NULL
    GROUP BY expected.trading_date,expected.expected_daily_rows"""


def status_payload(row: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return an explicit fail-closed readiness result from one aggregate row."""
    if not row:
        return {"state": "absent", "reason": "no canonical equity daily bars"}
    daily_rows = int(row["daily_rows"])
    expected_daily_rows = int(row.get("expected_daily_rows") or daily_rows)
    adjustment_rows = int(row["adjustment_rows"])
    limit_rows = int(row["limit_rows"])
    minimum_required_rows = math.ceil(expected_daily_rows * MINIMUM_ALL_A_COVERAGE_RATIO)
    cross_section_ready = daily_rows >= minimum_required_rows
    controls_ready = adjustment_rows == daily_rows and limit_rows == daily_rows
    ready = daily_rows > 0 and cross_section_ready and controls_ready
    if not cross_section_ready:
        reason = (
            "latest canonical equity daily bars cover "
            f"{daily_rows}/{expected_daily_rows} point-in-time all-A symbols; "
            f"requires at least {MINIMUM_ALL_A_COVERAGE_RATIO:.0%}"
        )
    elif not controls_ready:
        reason = "latest canonical equity daily bars are missing same-date adjustment or limit controls"
    else:
        reason = None
    return {
        "state": "ready" if ready else "blocked",
        "trade_date": str(row["trading_date"]),
        "daily_rows": daily_rows,
        "expected_daily_rows": expected_daily_rows,
        "minimum_required_rows": minimum_required_rows,
        "coverage_ratio": round(daily_rows / expected_daily_rows, 4) if expected_daily_rows else 0.0,
        "adjustment_rows": adjustment_rows,
        "limit_rows": limit_rows,
        "reason": reason,
    }


__all__ = ["EQUITY_DAILY_CONTROL_STATUS_SQL", "MINIMUM_ALL_A_COVERAGE_RATIO", "status_payload"]
