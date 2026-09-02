"""Read-only daily control-plane readiness policy.

Daily adjustment factors and price limits are equities-only controls.  Index
rows may coexist with the full-market daily table, but they intentionally do
not have ``adj_factor`` or ``stk_limit`` records and must not make the equity
decision gate appear unhealthy.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from .full_market_daily_controls_sync import sync as sync_full_market_daily_controls_isolated


MINIMUM_ALL_A_COVERAGE_RATIO = 0.95

#: The daily controls sync considers a longhuvip cross-section "usable" once
#: it covers at least this many symbols and its factor/limit controls reach
#: the same minimum coverage ratio as the equity readiness gate above.
LONGHU_MINIMUM_DAILY_ROWS = 3500


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


def daily_row_count(database: Any, trade_date: date) -> int:
    """Return a usable all-A daily cross-section count, otherwise fail closed.

    The controls synchronizer must never make a partially fetched daily date
    appear ready merely because its local rows have matching controls.  The
    expected population is the point-in-time all-A membership for this date.
    """
    with database.transaction() as connection:
        row = connection.execute(
            """WITH expected AS (
                   SELECT count(DISTINCT symbol)::int AS expected_rows
                     FROM quant.universe_membership_history
                    WHERE universe_key='all_a' AND effective_from<=%s
                      AND (effective_to IS NULL OR effective_to>=%s)
               ), actual AS (
                   SELECT count(DISTINCT bar.symbol)::int AS actual_rows
                     FROM quant.canonical_bars_daily bar
                     JOIN quant.universe_membership_history membership
                       ON membership.universe_key='all_a' AND membership.symbol=bar.symbol
                      AND membership.effective_from<=%s
                      AND (membership.effective_to IS NULL OR membership.effective_to>=%s)
                    WHERE bar.trading_date=%s AND bar.quality_status IN ('fresh','partial')
               ) SELECT expected_rows,actual_rows FROM expected CROSS JOIN actual""",
            (trade_date, trade_date, trade_date, trade_date, trade_date),
        ).fetchone()
    expected = int((row or {}).get("expected_rows") or 0)
    actual = int((row or {}).get("actual_rows") or 0)
    return actual if expected and actual >= math.ceil(expected * MINIMUM_ALL_A_COVERAGE_RATIO) else 0


@dataclass(frozen=True)
class DailyControlPlaneSyncDependencies:
    database: Any
    longhu_vendor_configured: Callable[[], bool]
    run_database: Callable[..., Awaitable[Any]]
    call_tushare_api: Callable[..., Awaitable[Any]]
    parse_tushare_date: Callable[[Any], date | None]
    persist_tushare_rows: Callable[..., Any]
    persist_blocked: Callable[[str, Exception], None]
    safe_error_detail: Callable[[str, int], str]
    executor_saturated_error: type[BaseException]
    record_provider_success: Callable[..., None]
    record_provider_failure: Callable[..., None]
    record_provider_api_capability: Callable[..., None]


def _longhu_control_status(database: Any, trade_date: date) -> dict[str, Any] | None:
    with database.transaction() as connection:
        row = connection.execute(
            """WITH daily AS (
                   SELECT count(*)::int AS rows FROM quant.canonical_bars_daily
                    WHERE trading_date=%s AND selected_provider='longhuvip_composite'
                 ), factors AS (
                   SELECT count(DISTINCT symbol)::int AS rows FROM quant.daily_adjustment_factors
                    WHERE trading_date=%s AND provider='longhuvip_composite'
                 ), limits AS (
                   SELECT count(DISTINCT symbol)::int AS rows FROM quant.daily_trade_limits
                    WHERE trading_date=%s AND provider='longhuvip_composite'
                 ) SELECT daily.rows AS daily_rows,factors.rows AS factor_rows,
                          limits.rows AS limit_rows FROM daily,factors,limits""",
            (trade_date, trade_date, trade_date),
        ).fetchone()
    daily_rows = int((row or {}).get("daily_rows") or 0)
    factor_rows = int((row or {}).get("factor_rows") or 0)
    limit_rows = int((row or {}).get("limit_rows") or 0)
    minimum_control_rows = math.ceil(daily_rows * MINIMUM_ALL_A_COVERAGE_RATIO)
    if daily_rows >= LONGHU_MINIMUM_DAILY_ROWS and factor_rows >= minimum_control_rows and limit_rows >= minimum_control_rows:
        return {
            "status": "completed", "trade_date": str(trade_date),
            "provider": "longhuvip_composite", "expected_daily_rows": daily_rows,
            "rows": {"adj_factor": factor_rows, "stk_limit": limit_rows, "suspend_d": 0},
            "quality_note": (
                "adj_factor is same-day identity only; limits are board-rule derived and retain IPO/resumption warnings"
            ),
        }
    return None


async def sync_full_market_daily_controls(
    trade_date: date, dependencies: DailyControlPlaneSyncDependencies,
) -> dict[str, Any]:
    """Fill same-date adjustment, limit and suspension controls after daily sync."""
    if dependencies.longhu_vendor_configured():
        ready = await dependencies.run_database(
            lambda: _longhu_control_status(dependencies.database, trade_date))
        if ready:
            return ready
    return await sync_full_market_daily_controls_isolated(
        trade_date,
        expected_daily_rows=lambda date_: daily_row_count(dependencies.database, date_),
        call_tushare_api=dependencies.call_tushare_api,
        parse_date=dependencies.parse_tushare_date,
        persist_tushare_rows=dependencies.persist_tushare_rows,
        persist_blocked=dependencies.persist_blocked,
        run_database_blocking=dependencies.run_database,
        db=dependencies.database,
        safe_error_detail=dependencies.safe_error_detail,
        executor_saturated_error=dependencies.executor_saturated_error,
        record_provider_success=dependencies.record_provider_success,
        record_provider_failure=dependencies.record_provider_failure,
        record_provider_api_capability=dependencies.record_provider_api_capability,
    )


__all__ = ["EQUITY_DAILY_CONTROL_STATUS_SQL", "MINIMUM_ALL_A_COVERAGE_RATIO", "status_payload"]
