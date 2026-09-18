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

from psycopg.types.json import Json

from .daily_control_plane import MINIMUM_ALL_A_COVERAGE_RATIO, daily_row_count
from .full_market_daily_controls_sync import COVERAGE_BLOCK_REASON, sync as sync_daily_controls
from .tushare_normalization import (
    PROMOTABLE_FACTOR_PROVIDER_PREFIX,
    promotable_factor_predicate_sql,
)

#: One trading date is considered still pending while fewer than this share of
#: its settled bars carry a factor.  Same ratio as the equity readiness gate:
#: a handful of symbols that genuinely have no factor must not keep a date on
#: the work list forever.
PENDING_COVERAGE_RATIO = MINIMUM_ALL_A_COVERAGE_RATIO

CHINA = ZoneInfo("Asia/Shanghai")

#: Release/CI guard: no bar row may carry a factor that no promotable evidence
#: supports.  "Promotable" is the same rule the writers obey -- a tushare route
#: whose declared semantics are absent or cumulative -- so the guard is not
#: limited to the one placeholder marker this branch happened to find.  A
#: vendor that simply omits ``factor_semantics`` is caught by the same query.
#: Must return 0.  ``{table}`` is injected by :func:`identity_factor_leak_sql`
#: from a pinned allowlist -- never from caller input.
IDENTITY_FACTOR_LEAK_SQL_TEMPLATE = f"""SELECT count(*)::bigint AS identity_leaks
     FROM quant.{{table}} bar
    WHERE bar.adj_factor IS NOT NULL
      -- Some factor evidence exists for this bar, so the value plausibly came
      -- from it (a bar carried forward with no factor row at all is a
      -- different question and is not this guard's subject) ...
      AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors evidence
                   WHERE evidence.symbol = bar.symbol AND evidence.trading_date = bar.trading_date)
      -- ... and none of that evidence is a row this repository would ever
      -- promote onto a bar.
      AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors promotable
                   WHERE promotable.symbol = bar.symbol AND promotable.trading_date = bar.trading_date
                     AND promotable.provider LIKE '{PROMOTABLE_FACTOR_PROVIDER_PREFIX}%'
                     AND {promotable_factor_predicate_sql("promotable", "raw")})"""

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
#: "This row is a REAL cumulative factor": the same promotion rule the writers
#: obey, expressed against ``quant.daily_adjustment_factors`` under the alias
#: ``factor``.  Written for statements that carry parameters, so the ``LIKE``
#: wildcard is doubled for psycopg.
REAL_FACTOR_PREDICATE_SQL = (
    f"factor.provider LIKE '{PROMOTABLE_FACTOR_PROVIDER_PREFIX}%%' "
    f"AND {promotable_factor_predicate_sql('factor', 'raw')}"
)

PENDING_DATES_SQL = f"""WITH settled AS (
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
       SELECT DISTINCT factor.trading_date, factor.symbol
         FROM quant.daily_adjustment_factors factor
        WHERE factor.trading_date BETWEEN %s AND %s
          AND {REAL_FACTOR_PREDICATE_SQL}
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


#: Durable ledger for a date the controls sync refuses on coverage grounds.
#: ``quant.automation_runs`` already is this repository's run ledger, so no new
#: table is introduced: one row per retired trading date, keyed by ``run_key``.
BLOCKED_DATE_TASK_KEY = "adjustment_factor_maintenance.blocked_date"
BLOCKED_DATE_RUN_KEY_PREFIX = "adjustment-factor-blocked"

#: After this many CONSECUTIVE coverage-blocked runs, a date drops off the work
#: list.  ``pending_dates`` recomputes the same list from real factor coverage
#: on every run, so without this a permanently thin session would be retried --
#: and, before this change, reported as a failure -- every night forever.
MAX_CONSECUTIVE_BLOCKED_RUNS = 5

_RECORD_BLOCKED_DATE_SQL = """
INSERT INTO quant.automation_runs(
        task_key,run_key,cadence,as_of_date,status,methodology_version,input_summary,output_summary,finished_at)
     VALUES(%s,%s,'daily',%s,'blocked',%s,%s,%s,now())
ON CONFLICT(run_key) DO UPDATE SET
     status='blocked', finished_at=now(), updated_at=now(),
     output_summary = quant.automation_runs.output_summary || jsonb_build_object(
         'consecutive_blocked_runs',
         coalesce((quant.automation_runs.output_summary->>'consecutive_blocked_runs')::int,0)+1,
         'reason', EXCLUDED.output_summary->>'reason',
         'last_blocked_at', now()::text)
RETURNING output_summary"""

_RETIRED_DATES_SQL = """SELECT as_of_date FROM quant.automation_runs
     WHERE task_key=%s AND run_key = ANY(%s)
       AND coalesce((output_summary->>'consecutive_blocked_runs')::int,0) >= %s"""


def blocked_date_run_key(trade_date: date) -> str:
    """One durable ledger row per trading date."""
    return f"{BLOCKED_DATE_RUN_KEY_PREFIX}:{trade_date}"


def retired_dates(connection: Any, dates: list[date]) -> set[date]:
    """Dates that have been coverage-blocked often enough to drop off the list."""
    if not dates:
        return set()
    rows = connection.execute(
        _RETIRED_DATES_SQL,
        (BLOCKED_DATE_TASK_KEY, [blocked_date_run_key(value) for value in dates],
         MAX_CONSECUTIVE_BLOCKED_RUNS),
    ).fetchall()
    return {row["as_of_date"] for row in rows}


def record_blocked_date(connection: Any, trade_date: date, reason: str) -> dict[str, Any]:
    """Count one coverage-blocked run, and retire the date once at the limit.

    Returns the ledger state for this date.  The retirement receipt is written
    exactly once (``retirement_receipt_written`` in the same ledger row), so a
    retired date is loud on the run that retires it and silent afterwards
    rather than alerting every night.
    """
    summary = connection.execute(
        _RECORD_BLOCKED_DATE_SQL,
        (BLOCKED_DATE_TASK_KEY, blocked_date_run_key(trade_date), trade_date,
         BLOCKED_DATE_TASK_KEY, Json({"trade_date": str(trade_date)}),
         Json({"consecutive_blocked_runs": 1, "reason": reason,
               "last_blocked_at": None})),
    ).fetchone()["output_summary"]
    blocked_runs = int(summary.get("consecutive_blocked_runs") or 0)
    state = {
        "consecutive_blocked_runs": blocked_runs,
        "retired": blocked_runs >= MAX_CONSECUTIVE_BLOCKED_RUNS,
        "retirement_receipt_written": False,
    }
    if state["retired"] and not summary.get("retirement_receipt_written"):
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,severity,code,message,details,trading_date)
                   VALUES('adj_factor','warning','adjustment_factor_date_retired',%s,%s,%s)""",
            (f"{trade_date} was refused by the daily-controls coverage gate on "
             f"{blocked_runs} consecutive adjustment-factor maintenance runs and has been "
             "dropped from the work list; its adj_factor stays NULL until the date's daily "
             "cross-section is repaired and the ledger row is cleared",
             Json({"trade_date": str(trade_date), "reason": reason,
                   "consecutive_blocked_runs": blocked_runs,
                   "run_key": blocked_date_run_key(trade_date)}),
             trade_date),
        )
        connection.execute(
            """UPDATE quant.automation_runs
                  SET output_summary = output_summary || jsonb_build_object(
                          'retirement_receipt_written', true, 'retired_at', now()::text),
                      updated_at = now()
                WHERE run_key=%s""",
            (blocked_date_run_key(trade_date),),
        )
        state["retirement_receipt_written"] = True
    return state


def clear_blocked_date(connection: Any, trade_date: date) -> None:
    """Reset the consecutive-block counter once a date is fetched successfully."""
    connection.execute(
        """UPDATE quant.automation_runs
              SET status='completed', finished_at=now(), updated_at=now(),
                  output_summary = output_summary || jsonb_build_object(
                      'consecutive_blocked_runs', 0, 'retirement_receipt_written', false,
                      'cleared_at', now()::text)
            WHERE run_key=%s""",
        (blocked_date_run_key(trade_date),),
    )


#: Run-level outcomes.  Only :data:`FAILED_STATUS` is a non-zero exit: a date
#: the coverage gate refuses is this job reporting on someone else's missing
#: daily cross-section, not a failure of the factor lane.
FAILED_STATUS = "failed"
SUCCESS_STATUSES = ("completed", "planned", "unchanged", "skipped")

#: A shorter window for the non-gating post-close invocation: the evening run
#: repairs the session that just landed (and the handful before it), while the
#: 04:30 maintenance task owns the full backlog.
POST_CLOSE_LOOKBACK_DAYS = 5


def _classify(outcome: dict[str, Any]) -> str:
    """Map one controls-sync result onto completed / skipped / failed.

    ``full_market_daily_controls_sync`` reports every refusal as ``blocked``;
    ``blocked_by`` says whether the cross-section was simply not good enough
    for a promotion (``coverage``) or whether the provider itself errored.
    """
    status = str(outcome.get("status") or "")
    if status in {"completed", "unchanged"}:
        return "completed"
    if status == "blocked" and outcome.get("blocked_by") == COVERAGE_BLOCK_REASON:
        return "skipped"
    return "failed"


async def sync(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int = 30, dry_run: bool = False, today: date | None = None,
) -> dict[str, Any]:
    """Fetch the missing cumulative factors for every pending settled date.

    ``dry_run`` resolves and reports the work list without issuing a single
    provider call or write, which is what the one-time repair runbook uses to
    confirm the scope before anything touches the database.

    A date the daily-controls coverage gate refuses is reported as *skipped*
    with its reason and does not fail the run: the factor lane cannot repair a
    thin daily cross-section, and a scheduled task that exits non-zero for a
    condition it cannot fix would alert every night forever.  After
    :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` such runs the date drops off the work
    list with a one-time durable receipt.  Only a provider error or an
    exception makes the run itself fail.
    """
    dates = await dependencies.run_database(functools.partial(
        pending_dates, dependencies.database, lookback_days=lookback_days, today=today,
    ))
    retired = await dependencies.run_database(functools.partial(
        _retired_dates, dependencies.database, dates,
    ))
    work = [value for value in dates if value not in retired]
    plan: dict[str, Any] = {
        "status": "planned" if dry_run else "completed",
        "lookback_days": int(lookback_days),
        "pending_dates": [str(value) for value in dates],
        "retired_dates": [str(value) for value in dates if value in retired],
        "dry_run": bool(dry_run),
    }
    if dry_run or not work:
        plan["results"] = []
        if not work:
            plan["status"] = "planned" if dry_run else "unchanged"
        return plan

    results: list[dict[str, Any]] = []
    for trade_date in work:
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
        outcome["outcome"] = _classify(outcome)
        if outcome["outcome"] == "skipped":
            outcome["ledger"] = await dependencies.run_database(functools.partial(
                _record_blocked_date, dependencies.database, trade_date,
                str(outcome.get("reason") or "coverage gate refused this date"),
            ))
        elif outcome["outcome"] == "completed":
            await dependencies.run_database(functools.partial(
                _clear_blocked_date, dependencies.database, trade_date,
            ))
        results.append(outcome)

    counts = {name: sum(1 for item in results if item["outcome"] == name)
              for name in ("completed", "skipped", "failed")}
    plan["results"] = results
    plan["completed_dates"] = counts["completed"]
    plan["skipped_dates"] = counts["skipped"]
    plan["failed_dates"] = counts["failed"]
    plan["status"] = (
        FAILED_STATUS if counts["failed"] else
        "skipped" if not counts["completed"] else
        "completed"
    )
    return plan


async def post_close_sync(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int = POST_CLOSE_LOOKBACK_DAYS, today: date | None = None,
) -> dict[str, Any]:
    """Run the factor lane as a NON-GATING post-close stage.

    The stage runs after the market refresh and reports its own status, but it
    never contributes to ``controls_ready`` and is never a dependency of
    another stage: the adjustment-factor provider is a separate route whose
    availability must not be able to push the evening pipeline to ``partial``.
    ``post_close_refresh.NON_GATING_STAGES`` is what enforces that, and this
    payload states it so the receipt is self-describing.
    """
    result = await sync(dependencies, lookback_days=lookback_days, today=today)
    return {
        **result, "non_gating": True,
        "reason": "adjustment factors are repaired on their own lane; this stage never gates the pipeline",
    }


def _retired_dates(database: Any, dates: list[date]) -> set[date]:
    with database.transaction() as connection:
        return retired_dates(connection, dates)


def _record_blocked_date(database: Any, trade_date: date, reason: str) -> dict[str, Any]:
    with database.transaction() as connection:
        return record_blocked_date(connection, trade_date, reason)


def _clear_blocked_date(database: Any, trade_date: date) -> None:
    with database.transaction() as connection:
        clear_blocked_date(connection, trade_date)


__all__ = [
    "AdjustmentFactorMaintenanceDependencies", "BLOCKED_DATE_TASK_KEY", "FAILED_STATUS",
    "GUARDED_BAR_TABLES", "IDENTITY_FACTOR_LEAK_SQL_TEMPLATE", "MAX_CONSECUTIVE_BLOCKED_RUNS",
    "PENDING_COVERAGE_RATIO", "PENDING_DATES_SQL", "POST_CLOSE_LOOKBACK_DAYS",
    "REAL_FACTOR_PREDICATE_SQL", "SUCCESS_STATUSES",
    "blocked_date_run_key", "china_today", "clear_blocked_date", "identity_factor_leak_sql",
    "pending_dates", "pending_dates_between", "post_close_sync", "record_blocked_date",
    "retired_dates", "sync",
]
