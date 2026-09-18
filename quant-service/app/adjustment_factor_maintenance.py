"""Maintenance-window repair lane for cumulative daily adjustment factors.

``quant.canonical_bars_daily.adj_factor`` is a cumulative (hfq-style)
corporate-action factor: ``close * adj_factor`` must be comparable across
dates.  The settled full-market cross-section and the factor now come from
different providers, so a trading date can legitimately land with complete
bars and limits while its factors are still missing.  Filling that gap is
this module's only job.

It deliberately runs OUTSIDE the post-close pipeline (the 04:00-08:00
maintenance window, or a one-time repair), because the factor route is a
separate provider whose availability must not be able to delay or fail the
evening close path.  Nothing here interprets prices; it re-uses
``full_market_daily_controls_sync.sync`` with ``apis=('adj_factor',)`` so the
fetch, coverage gate, provenance receipts and promotion stay in exactly one
place.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .daily_control_plane import MINIMUM_ALL_A_COVERAGE_RATIO, daily_row_count
from .full_market_daily_controls_sync import sync as sync_daily_controls

#: One trading date is considered still pending while fewer than this share of
#: its settled bars carry a factor.  Same ratio as the equity readiness gate:
#: a handful of symbols that genuinely have no factor must not keep a date on
#: the work list forever.
PENDING_COVERAGE_RATIO = MINIMUM_ALL_A_COVERAGE_RATIO

CHINA = ZoneInfo("Asia/Shanghai")

#: Release/CI guard: no bar row may carry a factor whose only evidence is a
#: vendor same-day identity placeholder.  Must return 0.  ``%s`` is the bar
#: table name, injected by :func:`identity_factor_leak_sql` from a pinned
#: allowlist -- never from caller input.
IDENTITY_FACTOR_LEAK_SQL_TEMPLATE = """SELECT count(*)::bigint AS identity_leaks
     FROM quant.{table} bar
    WHERE bar.adj_factor IS NOT NULL
      AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors ident
                   WHERE ident.symbol = bar.symbol AND ident.trading_date = bar.trading_date
                     AND ident.raw->>'factor_semantics' = 'same_day_identity_only')
      AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors f
                   WHERE f.symbol = bar.symbol AND f.trading_date = bar.trading_date
                     AND f.provider LIKE 'tushare%')"""

#: The only tables the leak guard may be pointed at.
GUARDED_BAR_TABLES = ("canonical_bars_daily", "market_bars_daily")


def identity_factor_leak_sql(table: str = "canonical_bars_daily") -> str:
    """Return the guard query for one pinned bar table."""
    if table not in GUARDED_BAR_TABLES:
        raise ValueError(f"table must be one of {GUARDED_BAR_TABLES}; got {table!r}")
    return IDENTITY_FACTOR_LEAK_SQL_TEMPLATE.format(table=table)


#: A settled bar counts as covered only when a REAL cumulative factor exists
#: for it -- the question is asked of ``quant.daily_adjustment_factors``, not of
#: ``canonical_bars_daily.adj_factor``, so the work list is correct both before
#: the one-time repair (while identity placeholders still sit on the bars and
#: would otherwise make every damaged date look complete) and after it.
PENDING_DATES_SQL = """WITH settled AS (
       SELECT bar.trading_date, bar.symbol FROM quant.canonical_bars_daily bar
        WHERE bar.trading_date BETWEEN %s AND %s
          AND bar.quality_status IN ('fresh','partial')
          -- Only an exchange session can have a factor cross-section.  Stray
          -- rows on a non-trading date (production carries 12 of them on
          -- 2026-08-30, a Sunday) would otherwise sit on the work list
          -- forever, since every fetch for them is refused before it starts.
          AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                       WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)
   ), factored AS (
       SELECT DISTINCT trading_date, symbol FROM quant.daily_adjustment_factors
        WHERE trading_date BETWEEN %s AND %s
          AND provider LIKE 'tushare%%'
          AND coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'
   ) SELECT settled.trading_date
       FROM settled LEFT JOIN factored USING (trading_date, symbol)
      GROUP BY settled.trading_date
     HAVING count(*) FILTER (WHERE factored.symbol IS NOT NULL) < ceil(%s * count(*))
      ORDER BY settled.trading_date"""


@dataclass(frozen=True)
class AdjustmentFactorMaintenanceDependencies:
    """Everything one factor-repair run needs; no provider client is owned here."""

    database: Any
    run_database: Callable[..., Awaitable[Any]]
    call_tushare_api: Callable[..., Awaitable[Any]]
    parse_tushare_date: Callable[[Any], date | None]
    persist_tushare_rows: Callable[..., int]
    persist_blocked: Callable[..., Any]
    safe_error_detail: Callable[[str, int], str]
    executor_saturated_error: type[BaseException]
    record_provider_success: Callable[..., Any]
    record_provider_failure: Callable[..., Any]
    record_provider_api_capability: Callable[..., Any]


def china_today(now: datetime | None = None) -> date:
    """Exchange-local calendar date; every trading date in this repo is CST."""
    return (now or datetime.now(CHINA)).astimezone(CHINA).date()


def pending_dates_between(
    connection: Any, start_date: date, end_date: date,
    *, minimum_ratio: float = PENDING_COVERAGE_RATIO,
) -> list[date]:
    """Settled trading dates in the window whose factor coverage is incomplete."""
    rows = connection.execute(
        PENDING_DATES_SQL,
        (start_date, end_date, start_date, end_date, float(minimum_ratio)),
    ).fetchall()
    return [row["trading_date"] for row in rows]


def pending_dates(
    database: Any, *, lookback_days: int = 30, today: date | None = None,
    minimum_ratio: float = PENDING_COVERAGE_RATIO,
) -> list[date]:
    """Read-only work list for the maintenance job and the readiness labels."""
    if lookback_days < 0:
        raise ValueError("lookback_days must not be negative")
    end_date = today or china_today()
    start_date = end_date - timedelta(days=lookback_days)
    with database.transaction() as connection:
        return pending_dates_between(
            connection, start_date, end_date, minimum_ratio=minimum_ratio)


async def sync(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int = 30, dry_run: bool = False, today: date | None = None,
) -> dict[str, Any]:
    """Fetch the missing cumulative factors for every pending settled date.

    ``dry_run`` resolves and reports the work list without issuing a single
    provider call or write, which is what the one-time repair runbook uses to
    confirm the scope before anything touches the database.
    """
    dates = await dependencies.run_database(functools.partial(
        pending_dates, dependencies.database, lookback_days=lookback_days, today=today,
    ))
    plan = {
        "status": "planned" if dry_run else "completed",
        "lookback_days": int(lookback_days),
        "pending_dates": [str(value) for value in dates],
        "dry_run": bool(dry_run),
    }
    if dry_run or not dates:
        plan["results"] = []
        if not dates:
            plan["status"] = "planned" if dry_run else "unchanged"
        return plan

    results: list[dict[str, Any]] = []
    for trade_date in dates:
        try:
            outcome = await sync_daily_controls(
                trade_date,
                apis=("adj_factor",),
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
        except Exception as error:  # noqa: BLE001 - one date must not end the run
            outcome = {
                "status": "failed", "trade_date": str(trade_date),
                "reason": dependencies.safe_error_detail(str(error), 500),
            }
        outcome.setdefault("trade_date", str(trade_date))
        results.append(outcome)

    completed = [item for item in results if item.get("status") == "completed"]
    plan["results"] = results
    plan["completed_dates"] = len(completed)
    plan["status"] = "completed" if len(completed) == len(results) else "partial"
    return plan


__all__ = [
    "AdjustmentFactorMaintenanceDependencies", "GUARDED_BAR_TABLES",
    "IDENTITY_FACTOR_LEAK_SQL_TEMPLATE", "PENDING_COVERAGE_RATIO", "PENDING_DATES_SQL",
    "china_today", "identity_factor_leak_sql", "pending_dates", "pending_dates_between", "sync",
]
