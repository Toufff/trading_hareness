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
    *, minimum_ratio: float = PENDING_COVERAGE_RATIO, include_retired: bool = False,
) -> list[date]:
    """Settled trading dates in the window whose factor coverage is incomplete.

    "Pending" means *a repair is still queued for this date*.  A date the
    blocked-date ledger has RETIRED (coverage refused on
    :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` separate days) has dropped off the
    work list and will not be fetched again until its daily cross-section is
    repaired and the ledger row is cleared, so reporting it as pending
    promises a repair that nobody is going to attempt.  The ledger is therefore consulted here, at the one
    place every caller goes through, rather than in each caller.

    ``include_retired=True`` returns the raw coverage list and is for the one
    caller that reports the two sets separately: :func:`sync`, which needs the
    retired dates in order to name them in its own receipt.
    """
    rows = connection.execute(
        PENDING_DATES_SQL,
        (start_date, end_date, start_date, end_date, float(minimum_ratio)),
    ).fetchall()
    dates = [row["trading_date"] for row in rows]
    if include_retired:
        return dates
    # ``retired_dates`` is defined below with the rest of the ledger; it asks
    # nothing of the database when the coverage list is empty.
    retired = retired_dates(connection, dates)
    return [value for value in dates if value not in retired]


def pending_and_retired_dates_between(
    connection: Any, start_date: date, end_date: date,
    *, minimum_ratio: float = PENDING_COVERAGE_RATIO,
) -> tuple[list[date], dict[date, dict[str, Any]]]:
    """Split one window's coverage list into "still queued" and "retired".

    Readiness labels need both halves: a date that is queued is expected to
    arrive, a retired one never will, and the caller must be able to say which
    with the ledger's own evidence (run_key + reason) rather than guessing.
    """
    dates = pending_dates_between(
        connection, start_date, end_date, minimum_ratio=minimum_ratio, include_retired=True)
    retired = retired_date_details(connection, dates)
    return [value for value in dates if value not in retired], retired


def pending_dates(
    database: Any, *, lookback_days: int = 30, today: date | None = None,
    minimum_ratio: float = PENDING_COVERAGE_RATIO, include_retired: bool = False,
) -> list[date]:
    """Read-only work list for the maintenance job and the readiness labels.

    ``include_retired`` is passed straight through to
    :func:`pending_dates_between` and defaults to excluding retired dates, so a
    caller that has not thought about the ledger cannot promise a repair that
    has already been given up on.
    """
    if lookback_days < 0:
        raise ValueError("lookback_days must not be negative")
    end_date = today or china_today()
    start_date = end_date - timedelta(days=lookback_days)
    with database.transaction() as connection:
        return pending_dates_between(
            connection, start_date, end_date, minimum_ratio=minimum_ratio,
            include_retired=include_retired)


#: Durable ledger for a date the controls sync refuses on coverage grounds.
#: ``quant.automation_runs`` already is this repository's run ledger, so no new
#: table is introduced: one row per retired trading date, keyed by ``run_key``.
BLOCKED_DATE_TASK_KEY = "adjustment_factor_maintenance.blocked_date"
BLOCKED_DATE_RUN_KEY_PREFIX = "adjustment-factor-blocked"

#: After this many CONSECUTIVE coverage-blocked LEDGER DAYS -- five separate
#: evenings, not five stage invocations -- a date drops off the work list.
#: ``pending_dates`` recomputes the same list from real factor coverage on
#: every run, so without this a permanently thin session would be retried --
#: and, before this change, reported as a failure -- every night forever.
#:
#: The unit is a day because the lane runs many times per evening: the
#: post-close pipeline task repeats every ``RetryIntervalMinutes`` until ~22:40
#: (about a dozen invocations), and the 04:30 maintenance task runs once more
#: over the same backlog.  Counting invocations would retire a date inside a
#: single evening -- two and a half hours instead of five days.
MAX_CONSECUTIVE_BLOCKED_RUNS = 5

#: A ledger day starts at this CST hour.  The window 12:00 -> 12:00 holds one
#: whole evening: the post-close pipeline (15:30-22:40, all repetitions) plus
#: the 04:30 maintenance run that works the same backlog before the next
#: session opens.  All of them share one ``blocked_days`` entry, so
#: :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` really does mean five evenings.
BLOCKED_LEDGER_DAY_BOUNDARY_HOUR = 12

#: The ledger day of "now", as SQL and as Python.  The SQL expression is
#: authoritative (the counter is decided inside one statement, so two
#: concurrent invocations cannot both count); :func:`blocked_ledger_day` is the
#: same rule for callers and tests, and ``BlockedDateLedgerPostgresTests`` pins
#: the two to each other on real PostgreSQL.
_LEDGER_DAY_SQL = (
    f"((now() AT TIME ZONE 'Asia/Shanghai') "
    f"- interval '{BLOCKED_LEDGER_DAY_BOUNDARY_HOUR} hours')::date")
_LEDGER_DAY_JSON_SQL = f"to_jsonb({_LEDGER_DAY_SQL}::text)"

#: The days this date has already been blocked on.  A row written before this
#: key existed has no ``blocked_days`` at all, so it reads as an empty array
#: and keeps its counter: an upgrade never resets nor double-counts a ledger.
_BLOCKED_DAYS_SQL = (
    "CASE WHEN jsonb_typeof(quant.automation_runs.output_summary->'blocked_days')='array' "
    "THEN quant.automation_runs.output_summary->'blocked_days' ELSE '[]'::jsonb END")
_COUNTED_TODAY_SQL = f"(({_BLOCKED_DAYS_SQL}) @> {_LEDGER_DAY_JSON_SQL})"
_BLOCKED_RUNS_SQL = (
    "coalesce((quant.automation_runs.output_summary->>'consecutive_blocked_runs')::int,0)")

_RECORD_BLOCKED_DATE_SQL = f"""
INSERT INTO quant.automation_runs(
        task_key,run_key,cadence,as_of_date,status,methodology_version,input_summary,output_summary,finished_at)
     VALUES(%s,%s,'daily',%s,'blocked',%s,%s,
            jsonb_build_object(
                'consecutive_blocked_runs', 1, 'reason', %s::text,
                'blocked_days', jsonb_build_array({_LEDGER_DAY_SQL}::text),
                'last_blocked_at', now()::text),
            now())
ON CONFLICT(run_key) DO UPDATE SET
     status='blocked', finished_at=now(), updated_at=now(),
     output_summary = quant.automation_runs.output_summary || jsonb_build_object(
         'consecutive_blocked_runs',
         CASE WHEN {_COUNTED_TODAY_SQL} THEN {_BLOCKED_RUNS_SQL} ELSE {_BLOCKED_RUNS_SQL}+1 END,
         'blocked_days',
         CASE WHEN {_COUNTED_TODAY_SQL} THEN ({_BLOCKED_DAYS_SQL})
              ELSE ({_BLOCKED_DAYS_SQL}) || {_LEDGER_DAY_JSON_SQL} END,
         'reason', EXCLUDED.output_summary->>'reason',
         'last_blocked_at', now()::text)
RETURNING output_summary"""

RETIRED_DATES_SQL = """SELECT as_of_date,run_key,output_summary FROM quant.automation_runs
     WHERE task_key=%s AND run_key = ANY(%s)
       AND coalesce((output_summary->>'consecutive_blocked_runs')::int,0) >= %s"""


def blocked_date_run_key(trade_date: date) -> str:
    """One durable ledger row per trading date."""
    return f"{BLOCKED_DATE_RUN_KEY_PREFIX}:{trade_date}"


def blocked_ledger_day(now: datetime | None = None) -> date:
    """The ledger day "now" belongs to -- the second half of the ledger's key.

    The ledger is keyed by ``(trading date, blocked-on day)``: every refusal of
    one trading date within one ledger day is the same entry, so the evening's
    repetitions count once.  The day boundary is
    :data:`BLOCKED_LEDGER_DAY_BOUNDARY_HOUR` CST rather than midnight so that
    the 04:30 maintenance run belongs to the evening whose backlog it is
    working, not to the next one.

    This is the Python twin of :data:`_LEDGER_DAY_SQL`; the statement decides
    the counter, this exists so a caller, a report or a test can name the same
    day without a database round trip.
    """
    local = (now or datetime.now(CHINA)).astimezone(CHINA)
    return (local - timedelta(hours=BLOCKED_LEDGER_DAY_BOUNDARY_HOUR)).date()


#: What a caller may say about a retired date without re-reading the ledger.
RETIRED_DATE_DEFAULT_REASON = "coverage gate refused this date"


def retired_date_details_from_rows(rows: Any) -> dict[date, dict[str, Any]]:
    """Shape :data:`RETIRED_DATES_SQL`'s rows into per-date ledger evidence.

    Separate from the query so a fixture harness -- ``scripts/verify-equity-
    control-recovery.py`` runs the same statement against ``pg_temp`` tables --
    reads a retired date exactly as production does, instead of hand-building a
    dict that can drift from this one.
    """
    details: dict[date, dict[str, Any]] = {}
    for row in rows:
        summary = dict(row.get("output_summary") or {})
        details[row["as_of_date"]] = {
            "run_key": row.get("run_key") or blocked_date_run_key(row["as_of_date"]),
            "reason": str(summary.get("reason") or RETIRED_DATE_DEFAULT_REASON),
            "consecutive_blocked_runs": int(summary.get("consecutive_blocked_runs") or 0),
            # The days the counter is made of, so a reader can check that a
            # retirement really took five evenings rather than one.
            "blocked_days": [str(value) for value in (summary.get("blocked_days") or [])],
            "retired_at": summary.get("retired_at"),
        }
    return details


def retired_date_details(connection: Any, dates: list[date]) -> dict[date, dict[str, Any]]:
    """Ledger evidence for every date that has dropped off the work list.

    Returned per date so a human-facing label can name *why* the repair is not
    queued -- the ledger ``run_key`` an operator clears to re-open it, the
    coverage reason the daily-controls gate gave, and the days on which it was
    refused.
    """
    if not dates:
        return {}
    return retired_date_details_from_rows(connection.execute(
        RETIRED_DATES_SQL,
        (BLOCKED_DATE_TASK_KEY, [blocked_date_run_key(value) for value in dates],
         MAX_CONSECUTIVE_BLOCKED_RUNS),
    ).fetchall())


def retired_dates(connection: Any, dates: list[date]) -> set[date]:
    """Dates that have been coverage-blocked often enough to drop off the list."""
    return set(retired_date_details(connection, dates))


def _blocked_days_receipt_phrase(blocked_runs: int, blocked_days: list[str]) -> str:
    """Name the days the counter is made of, WITHOUT claiming more than exists.

    The counter and the day list are deliberately out of step on one row: a
    ledger written before ``blocked_days`` existed carries its count from the
    invocation-counting era and gains its first recorded day only now, so a
    row can legitimately retire at ``consecutive_blocked_runs = 5`` while
    listing one day.  Printing that list inside "on 5 separate days (...)"
    reads as a contradiction and invites the reader to distrust the count, so
    the parenthesis says which of the two it is showing whenever they differ.
    """
    joined = ', '.join(blocked_days)
    if blocked_days and len(blocked_days) == blocked_runs:
        return f"({joined})"
    if blocked_days:
        return (f"(recorded days: {joined}; the earlier ones predate the day key and were "
                "counted per invocation)")
    return "(recorded days: none -- this row predates the day key)"


def record_blocked_date(connection: Any, trade_date: date, reason: str) -> dict[str, Any]:
    """Count one coverage-blocked DAY, and retire the date once at the limit.

    The ledger is keyed by ``(trading date, blocked-on day)``: the row is one
    trading date, and ``blocked_days`` inside it is the set of ledger days
    (see :func:`blocked_ledger_day`) on which that date was refused.  The
    counter is that set's size, so the evening's ~12 pipeline repetitions --
    and the 04:30 maintenance run working the same backlog -- move it by one,
    not by thirteen.  Every invocation still refreshes ``last_blocked_at`` and
    the reason, so the row stays a truthful record of the latest attempt.

    Returns the ledger state for this date.  The retirement receipt is written
    exactly once (``retirement_receipt_written`` in the same ledger row), so a
    retired date is loud on the day that retires it and silent afterwards
    rather than alerting every night.
    """
    summary = connection.execute(
        _RECORD_BLOCKED_DATE_SQL,
        (BLOCKED_DATE_TASK_KEY, blocked_date_run_key(trade_date), trade_date,
         BLOCKED_DATE_TASK_KEY, Json({"trade_date": str(trade_date)}), reason),
    ).fetchone()["output_summary"]
    blocked_runs = int(summary.get("consecutive_blocked_runs") or 0)
    blocked_days = [str(value) for value in (summary.get("blocked_days") or [])]
    state = {
        "consecutive_blocked_runs": blocked_runs,
        "blocked_days": blocked_days,
        "last_blocked_day": blocked_days[-1] if blocked_days else None,
        "retired": blocked_runs >= MAX_CONSECUTIVE_BLOCKED_RUNS,
        "retirement_receipt_written": False,
    }
    if state["retired"] and not summary.get("retirement_receipt_written"):
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,severity,code,message,details,trading_date)
                   VALUES('adj_factor','warning','adjustment_factor_date_retired',%s,%s,%s)""",
            (f"{trade_date} was refused by the daily-controls coverage gate on "
             f"{blocked_runs} separate days {_blocked_days_receipt_phrase(blocked_runs, blocked_days)} "
             "and has "
             "been dropped from the adjustment-factor work list; its adj_factor stays NULL until "
             "the date's daily cross-section is repaired and the ledger row is cleared",
             Json({"trade_date": str(trade_date), "reason": reason,
                   "consecutive_blocked_runs": blocked_runs,
                   "blocked_days": blocked_days,
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
    """Reset the consecutive-block counter once a date is fetched successfully.

    The retirement receipt is resolved in the same transaction: the issue said
    "this date is dropped from the work list", and a date that has just been
    fetched is back on it, so leaving the row open would keep an unresolvable
    warning in ``quant.data_quality_issues`` forever.
    """
    connection.execute(
        """UPDATE quant.automation_runs
              SET status='completed', finished_at=now(), updated_at=now(),
                  output_summary = output_summary || jsonb_build_object(
                      'consecutive_blocked_runs', 0, 'retirement_receipt_written', false,
                      'blocked_days', '[]'::jsonb, 'cleared_at', now()::text)
            WHERE run_key=%s""",
        (blocked_date_run_key(trade_date),),
    )
    connection.execute(
        """UPDATE quant.data_quality_issues SET resolved_at=now()
            WHERE code='adjustment_factor_date_retired' AND trading_date=%s
              AND resolved_at IS NULL""",
        (trade_date,),
    )


#: Run-level outcomes.  Only :data:`FAILED_STATUS` is a non-zero exit: a date
#: the coverage gate refuses is this job reporting on someone else's missing
#: daily cross-section, not a failure of the factor lane.
FAILED_STATUS = "failed"
SUCCESS_STATUSES = ("completed", "planned", "unchanged", "skipped")

#: A shorter window for the non-gating post-close invocation: the evening run
#: repairs the session that just landed (and the handful before it), while the
#: 04:30 maintenance task owns the full backlog.
#:
#: This is the FLOOR and the fallback, in calendar days, and it may never be
#: shorter than :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` lane evenings.  The
#: counter that retires a date counts ledger DAYS, and a date only reaches the
#: lane while it is inside this window, so a window shorter than the retirement
#: rule means the date silently leaves the work list before it can ever retire.
#: Under the only schedule that is actually installed -- the post-close
#: pipeline, ``-Daily -At 16:40`` with repetitions until ~22:40, which returns
#: early on Sat/Sun -- five lane evenings starting on a Friday end on the
#: following Thursday, seven calendar days later; with one missed evening of
#: slack, eight.  Fourteen is that with room for a holiday-shortened week, and
#: is what a host that has been off for a day or two still needs.
POST_CLOSE_LOOKBACK_DAYS = 14

#: The same window expressed in the unit that actually matters: exchange
#: sessions.  A date must still be inside the post-close window on the evening
#: of its :data:`MAX_CONSECUTIVE_BLOCKED_RUNS`-th refusal, and the lane reaches
#: it once per trading evening, so the window covers that many sessions plus
#: one evening of slack.  Counted from ``quant.market_trade_calendar`` so a
#: Spring Festival or National Day week -- nine calendar days with no session
#: -- widens the window instead of quietly dropping a date off it.
POST_CLOSE_LOOKBACK_SESSIONS = MAX_CONSECUTIVE_BLOCKED_RUNS + 1

#: The most recent :data:`POST_CLOSE_LOOKBACK_SESSIONS` open sessions, oldest
#: first.  ``DISTINCT`` because the calendar is keyed by ``(exchange, date)``
#: and the work-list query above is exchange-agnostic about the same table:
#: counting one date twice would halve the window.
POST_CLOSE_LOOKBACK_SESSIONS_SQL = """SELECT min(session) AS oldest_session,
            count(*)::int AS sessions
       FROM (SELECT DISTINCT calendar_date AS session FROM quant.market_trade_calendar
              WHERE is_open AND calendar_date<=%s
              ORDER BY 1 DESC LIMIT %s) recent"""


def post_close_lookback_days(
    today: date, oldest_session: date | None = None, *, sessions_found: int = 0,
    sessions: int = POST_CLOSE_LOOKBACK_SESSIONS, floor_days: int = POST_CLOSE_LOOKBACK_DAYS,
) -> int:
    """How far back the post-close invocation looks, in calendar days.

    Pure, so the rule can be pinned without a database.  The answer is never
    shorter than :data:`POST_CLOSE_LOOKBACK_DAYS` -- a calendar that has not
    been backfilled, or one whose last rows predate today, must widen the
    window, never narrow it -- and grows to whatever span the requested number
    of sessions actually occupies, which is what makes the window survive a
    holiday week.
    """
    if oldest_session is None or sessions_found < sessions or oldest_session > today:
        return floor_days
    return max(floor_days, (today - oldest_session).days)


def post_close_lookback_days_from_calendar(
    connection: Any, today: date, *,
    sessions: int = POST_CLOSE_LOOKBACK_SESSIONS, floor_days: int = POST_CLOSE_LOOKBACK_DAYS,
) -> int:
    """Resolve :func:`post_close_lookback_days` against the trade calendar."""
    row = connection.execute(POST_CLOSE_LOOKBACK_SESSIONS_SQL, (today, sessions)).fetchone() or {}
    return post_close_lookback_days(
        today, row.get("oldest_session"), sessions_found=int(row.get("sessions") or 0),
        sessions=sessions, floor_days=floor_days)


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
    :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` such DAYS -- the ledger counts one per
    evening however many times the stage runs -- the date drops off the work
    list with a one-time durable receipt.  Only a provider error or an
    exception makes the run itself fail.
    """
    # The raw coverage list: this job is the one caller that reports the
    # retired dates itself (``plan['retired_dates']`` below), so it asks for
    # them rather than letting the helper drop them.
    dates = await dependencies.run_database(functools.partial(
        pending_dates, dependencies.database, lookback_days=lookback_days, today=today,
        include_retired=True,
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


#: Base explanation carried by every post-close receipt of this lane.
POST_CLOSE_NON_GATING_REASON = (
    "adjustment factors are repaired on their own lane; this stage never gates the pipeline")

#: The only lane verdicts whose post-close receipt may become DURABLE.
#:
#: ``post_close_refresh.record_stage_with_receipt`` normalizes any status it
#: does not recognise to ``completed`` and then refuses to run the stage again
#: for the same trade date, because a ``completed`` receipt is the pipeline's
#: "this work is done" marker.  The pipeline task repeats every
#: ``RetryIntervalMinutes`` until ~22:40, so a receipt that says ``completed``
#: while dates are still unrepaired burns the whole evening's retries.  Only a
#: run that left nothing behind may stick.
POST_CLOSE_TERMINAL_LANE_STATUSES = ("completed", "unchanged")


def post_close_stage_receipt(result: dict[str, Any]) -> dict[str, Any]:
    """Translate one lane result into a post-close stage status.

    Pure, so the mapping can be pinned without a database:

    ================================  ===========  ====================================
    lane verdict                      stage status durable receipt?
    ================================  ===========  ====================================
    ``completed`` (nothing left)      completed    yes -- the dates were repaired
    ``unchanged`` (no work found)     unchanged    yes -- nothing was pending
    ``completed`` with skipped dates  blocked      no  -- a mixed run still owes work
    ``skipped`` (coverage refused)    blocked      no  -- retry on the next repetition
    ``failed``                        failed       no  -- retry on the next repetition
    anything else (e.g. ``planned``)  blocked      no  -- never let an unknown stick
    ================================  ===========  ====================================

    ``blocked`` and ``failed`` are both statuses ``record_stage_with_receipt``
    recognises, so ``automation_runs`` keeps the real verdict instead of a
    normalized ``completed``, and ``start_or_resume_run`` re-opens the row on
    the next repetition.  Both also land in the run's
    ``non_gating_stages_needing_attention`` list, and neither can make the run
    ``partial`` -- ``NON_GATING_STAGES`` excludes this stage from
    ``deferred_stages``.
    """
    lane_status = str(result.get("status") or "")
    skipped = int(result.get("skipped_dates") or 0)
    failed = int(result.get("failed_dates") or 0)
    unrepaired = [str(item.get("trade_date")) for item in (result.get("results") or [])
                  if isinstance(item, dict) and item.get("outcome") in {"skipped", "failed"}]
    if lane_status == FAILED_STATUS or failed:
        status = "failed"
    elif lane_status == "skipped" or skipped:
        status = "blocked"
    elif lane_status in POST_CLOSE_TERMINAL_LANE_STATUSES:
        status = lane_status
    else:
        status = "blocked"
    retryable = status not in POST_CLOSE_TERMINAL_LANE_STATUSES
    reason = POST_CLOSE_NON_GATING_REASON
    if retryable:
        reason = (
            f"{reason}; {len(unrepaired) or skipped + failed} date(s) are still unrepaired "
            f"({', '.join(unrepaired) or 'see results'}), so this stage's durable receipt stays "
            "open and the next pipeline repetition re-evaluates them")
    return {"status": status, "lane_status": lane_status, "retryable": retryable,
            "unrepaired_dates": unrepaired, "reason": reason}


async def post_close_sync(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int | None = None, today: date | None = None,
) -> dict[str, Any]:
    """Run the factor lane as a NON-GATING post-close stage.

    The stage runs after the market refresh and reports its own status, but it
    never contributes to ``controls_ready`` and is never a dependency of
    another stage: the adjustment-factor provider is a separate route whose
    availability must not be able to push the evening pipeline to ``partial``.
    ``post_close_refresh.NON_GATING_STAGES`` is what enforces that, and this
    payload states it so the receipt is self-describing.

    The status is NOT the lane's own verdict: it is
    :func:`post_close_stage_receipt`'s translation of it into the durable
    receipt vocabulary, so a run that skipped a date for coverage is retried by
    the evening's later repetitions instead of being sealed as ``completed``.

    ``lookback_days=None`` (the default, and what production uses) resolves the
    window from the trade calendar via :func:`post_close_lookback_days_from_calendar`,
    so it always covers :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` lane evenings and a
    refused date can actually reach the retirement rule instead of falling out
    of the window first.  The resolved value is reported as ``lookback_days``
    in the receipt.
    """
    end_date = today or china_today()
    if lookback_days is None:
        lookback_days = await dependencies.run_database(functools.partial(
            _post_close_lookback_days, dependencies.database, end_date))
    result = await sync(dependencies, lookback_days=lookback_days, today=end_date)
    return {**result, **post_close_stage_receipt(result), "non_gating": True}


def _post_close_lookback_days(database: Any, today: date) -> int:
    with database.transaction() as connection:
        return post_close_lookback_days_from_calendar(connection, today)


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
    "AdjustmentFactorMaintenanceDependencies", "BLOCKED_DATE_TASK_KEY",
    "BLOCKED_LEDGER_DAY_BOUNDARY_HOUR", "FAILED_STATUS",
    "GUARDED_BAR_TABLES", "IDENTITY_FACTOR_LEAK_SQL_TEMPLATE", "MAX_CONSECUTIVE_BLOCKED_RUNS",
    "PENDING_COVERAGE_RATIO", "PENDING_DATES_SQL", "POST_CLOSE_LOOKBACK_DAYS",
    "POST_CLOSE_LOOKBACK_SESSIONS", "POST_CLOSE_LOOKBACK_SESSIONS_SQL",
    "POST_CLOSE_NON_GATING_REASON", "POST_CLOSE_TERMINAL_LANE_STATUSES",
    "REAL_FACTOR_PREDICATE_SQL", "RETIRED_DATES_SQL", "RETIRED_DATE_DEFAULT_REASON",
    "SUCCESS_STATUSES",
    "blocked_date_run_key", "blocked_ledger_day", "china_today", "clear_blocked_date",
    "identity_factor_leak_sql",
    "pending_and_retired_dates_between", "pending_dates", "pending_dates_between",
    "post_close_lookback_days", "post_close_lookback_days_from_calendar",
    "post_close_stage_receipt", "post_close_sync", "record_blocked_date",
    "retired_date_details", "retired_date_details_from_rows", "retired_dates", "sync",
]
