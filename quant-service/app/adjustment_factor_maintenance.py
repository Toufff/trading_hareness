"""Maintenance lane for cumulative daily adjustment factors -- longhu only.

``quant.canonical_bars_daily.adj_factor`` is a cumulative (hfq-style)
corporate-action factor: ``close * adj_factor`` must be comparable across
dates.  The settled full-market cross-section lands without a factor, so a
trading date can legitimately have complete bars and limits while its factors
are still missing.  Filling that gap is this module's only job.

Since 2026-09-19 the lane has NO tushare dependency (user decision: "完全去掉
tushare，用 longhu 来处理").  Every factor is derived by
``app.longhu_adjustment_factors`` from the licensed longhu daily kline
(``GetKLineDay_W14``: the vendor's CQ corporate-action record and its qfq
series) plus the bar's own pre_close, continuing each symbol's stored
cumulative series from its last real factor.  Derived rows are stored under
provider ``longhu_qfq_derived`` with their evidence; stored tushare factors are
only ever READ, as anchors and checkpoints.

The lane runs as a non-gating post-close stage and at 04:30 (the maintenance
window); :func:`repair` is the idempotent one-time backfill of the damaged
2026-08/09 window, with a read-only dry run.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from . import longhu_adjustment_factors as derivation
from .daily_control_plane import MINIMUM_ALL_A_COVERAGE_RATIO, daily_row_count
from .full_market_daily_controls_sync import COVERAGE_BLOCK_REASON, PROVIDER_BLOCK_REASON
from .tushare_normalization import promotable_factor_evidence_sql

#: One trading date is considered still pending while fewer than this share of
#: its settled bars carry a factor.  Same ratio as the equity readiness gate:
#: a handful of symbols that genuinely have no factor must not keep a date on
#: the work list forever.
PENDING_COVERAGE_RATIO = MINIMUM_ALL_A_COVERAGE_RATIO

CHINA = ZoneInfo("Asia/Shanghai")

#: Release/CI guard: no bar row may carry a factor that no promotable evidence
#: supports.  "Promotable" is the same rule the writers obey -- a tushare route
#: whose declared semantics are absent or cumulative, or the derived longhu
#: provider declaring cumulative semantics explicitly -- so the guard is not
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
                     AND {promotable_factor_evidence_sql("promotable", "raw")})"""

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
REAL_FACTOR_PREDICATE_SQL = promotable_factor_evidence_sql("factor", "raw").replace("%", "%%")

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
    """Everything one factor run needs; no tushare client, by construction.

    ``longhu_source`` returns an object with the licensed ``raw_call``
    contract (``longhu_vendor_source.intraday_source``); ``run_public`` runs
    the blocking all-symbol fetch in the bounded public-source executor.
    """

    database: Any
    run_database: Callable[..., Awaitable[Any]]
    longhu_source: Callable[[], Any]
    run_public: Callable[..., Awaitable[Any]]
    safe_error_detail: Callable[[str, int], str]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    control: Callable[..., Awaitable[Any]] | None = None


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
    """Map one per-date result onto completed / skipped / failed.

    ``blocked`` with ``blocked_by='coverage'`` means the date's own settled
    cross-section is not good enough (the lane cannot repair that); every
    other refusal -- the longhu fetch failed, the derivation covered too little
    of the date -- is this lane's failure.
    """
    status = str(outcome.get("status") or "")
    if status in {"completed", "unchanged"}:
        return "completed"
    if status == "blocked" and outcome.get("blocked_by") == COVERAGE_BLOCK_REASON:
        return "skipped"
    return "failed"


#: More longhu fetch failures than this share of the window's symbols is a
#: provider failure for the whole run (nothing is written); fewer, and the
#: failed symbols are derived from the bar pre_close alone and flagged
#: ``longhu_missing`` in their evidence.
MAX_FETCH_FAILURE_RATIO = 0.05
#: Budget for the one all-symbol longhu fetch (about 5,500 calls, ~95 s
#: measured 2026-09-19 with 16 workers).
LONGHU_FETCH_TIMEOUT_SECONDS = 1800
#: Budget for the window read and for each date's write transaction.
FACTOR_DB_TIMEOUT_SECONDS = 600
#: Parallel longhu calls.  No artificial throttle (the licensed wrapper has no
#: call-count or rate limit).
LONGHU_FETCH_WORKERS = 16


class FactorLaneSession:
    """One lane run: read the window once, fetch longhu once, plan once.

    The per-date loop in :func:`sync` asks this session for each date; the
    derivation is built lazily on the first date that needs it, over the
    whole work list, because one symbol's chain spans all of those dates.
    """

    def __init__(self, dependencies: AdjustmentFactorMaintenanceDependencies,
                 work: list[date], today: date, holes: list[date] | None = None,
                 mismatched: set[tuple[str, date]] | None = None) -> None:
        self.dependencies = dependencies
        self.work = sorted(work)
        #: Complete dates that still carry NULL factors of symbols with a
        #: factor history, or bar values no evidence row carries: filled
        #: there, never re-worked otherwise.
        self.holes = sorted(set(holes or ()) - set(work))
        #: (symbol, date) bars whose value equals no promotable evidence row.
        self.mismatched = set(mismatched or ())
        self.window = sorted(set(self.work) | set(self.holes))
        self.today = today
        self.plan: derivation.FactorPlan | None = None
        self.failure: str | None = None
        self.fetch_errors: dict[str, str] = {}
        self.written: dict[str, dict[str, int]] = {}
        self.available_at = dependencies.now()

    async def ensure_plan(self) -> derivation.FactorPlan | None:
        if self.plan is not None or self.failure is not None:
            return self.plan
        deps = self.dependencies
        inputs = await deps.run_database(
            functools.partial(_read_window, deps.database, self.window[0], self.window[-1]),
            timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
        sessions = derivation.sessions_to_fetch(inputs, self.today)
        try:
            source = deps.longhu_source()
            longhu, errors = await deps.run_public(
                derivation.fetch_longhu_evidence, source, sessions,
                timeout_seconds=LONGHU_FETCH_TIMEOUT_SECONDS, workers=LONGHU_FETCH_WORKERS)
        except Exception as error:  # noqa: BLE001 - reported as a provider failure
            self.failure = deps.safe_error_detail(f"longhu fetch failed: {error}", 500)
            return None
        self.fetch_errors = dict(errors)
        if sessions and len(errors) > MAX_FETCH_FAILURE_RATIO * len(sessions):
            self.failure = (f"longhu kline failed for {len(errors)} of {len(sessions)} symbols "
                            f"(limit {MAX_FETCH_FAILURE_RATIO:.0%})")
            return None
        self.plan = derivation.build_plan(
            inputs, longhu, write_dates=self.work, fetch_errors=errors, rederive_derived=False,
            null_only_dates=self.holes, also_replace=self.mismatched)
        return self.plan


async def repair_factor_date(session: FactorLaneSession, trade_date: date) -> dict[str, Any]:
    """Derive and write one pending date; the per-date unit of :func:`sync`."""
    deps = session.dependencies
    expected = await deps.run_database(daily_row_count, deps.database, trade_date)
    if expected <= 0:
        return {"status": "blocked", "trade_date": str(trade_date),
                "blocked_by": COVERAGE_BLOCK_REASON,
                "reason": "full-market daily bars are not ready"}
    plan = await session.ensure_plan()
    if plan is None:
        return {"status": "blocked", "trade_date": str(trade_date),
                "blocked_by": PROVIDER_BLOCK_REASON, "reason": session.failure}
    rows = plan.rows.get(trade_date, [])
    if len(rows) < math.ceil(expected * PENDING_COVERAGE_RATIO):
        return {"status": "blocked", "trade_date": str(trade_date),
                "blocked_by": PROVIDER_BLOCK_REASON,
                "reason": (f"derived factors for {len(rows)} symbols; the date needs at least "
                           f"{PENDING_COVERAGE_RATIO:.0%} of {expected}")}
    counts = await deps.run_database(
        functools.partial(_persist_date, deps.database, trade_date, rows,
                          list(plan.clear.get(trade_date, [])), session.available_at),
        timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    session.written[str(trade_date)] = counts
    derived_rows = sum(1 for row in rows if row.provider == derivation.PROVIDER_KEY)
    return {"status": "completed", "trade_date": str(trade_date), "expected_daily_rows": expected,
            "provider": derivation.PROVIDER_KEY, "rows": len(rows), "derived_rows": derived_rows,
            "writes": counts}


async def sync(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int = 30, dry_run: bool = False, today: date | None = None,
) -> dict[str, Any]:
    """Derive the missing cumulative factors for every pending settled date.

    ``dry_run`` resolves and reports the work list without issuing a single
    provider call or write.

    A date whose own settled cross-section is too thin is reported as
    *skipped* with its reason and does not fail the run: the factor lane
    cannot repair a thin daily cross-section, and a scheduled task that exits
    non-zero for a condition it cannot fix would alert every night forever.
    After :data:`MAX_CONSECUTIVE_BLOCKED_RUNS` such DAYS -- the ledger counts
    one per evening however many times the stage runs -- the date drops off
    the work list with a one-time durable receipt.  Only a longhu failure or
    an exception makes the run itself fail.
    """
    if not dry_run and getattr(dependencies, 'control', None) is not None:
        return await dependencies.control(dependencies, 'sync', sync,
            dict(lookback_days=lookback_days, dry_run=dry_run, today=today))
    # The raw coverage list: this job is the one caller that reports the
    # retired dates itself (``plan['retired_dates']`` below), so it asks for
    # them rather than letting the helper drop them.
    end_date = today or china_today()
    dates = await dependencies.run_database(functools.partial(
        pending_dates, dependencies.database, lookback_days=lookback_days, today=end_date,
        include_retired=True,
    ))
    retired = await dependencies.run_database(functools.partial(
        _retired_dates, dependencies.database, dates,
    ))
    work = [value for value in dates if value not in retired]
    holes = await dependencies.run_database(functools.partial(
        _hole_dates, dependencies.database, end_date - timedelta(days=lookback_days), end_date,
    ), timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    # Bars whose value no evidence row carries (a placeholder next to a real
    # tushare row): their dates are holes too, so the nightly lane heals them
    # even if the one-time repair has not been applied yet.
    mismatched = await dependencies.run_database(functools.partial(
        _value_mismatch_keys, dependencies.database, end_date - timedelta(days=lookback_days), end_date,
    ), timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    holes = sorted(set(holes) | {value for _symbol, value in mismatched})
    holes = [value for value in holes if value not in set(dates)]
    plan: dict[str, Any] = {
        "status": "planned" if dry_run else "completed",
        "lookback_days": int(lookback_days),
        "provider": derivation.PROVIDER_KEY,
        "pending_dates": [str(value) for value in dates],
        "hole_dates": [str(value) for value in holes],
        "value_mismatches": len(mismatched),
        "retired_dates": [str(value) for value in dates if value in retired],
        "dry_run": bool(dry_run),
    }
    if dry_run or not (work or holes):
        plan["results"] = []
        if not work:
            plan["status"] = "planned" if dry_run else "unchanged"
        return plan

    session = FactorLaneSession(dependencies, work, end_date, holes, set(mismatched))
    results: list[dict[str, Any]] = []
    for trade_date in work:
        try:
            outcome = await repair_factor_date(session, trade_date)
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

    # Holes on otherwise complete dates: filled where NULL, outside the
    # work-list ledger (a hole never retires a date and never fails the run).
    hole_fills: dict[str, Any] = {}
    if session.holes and session.plan is None and session.failure is None:
        await session.ensure_plan()
    if session.plan is not None:
        for value in session.holes:
            rows = session.plan.rows.get(value, [])
            # A held symbol's bar is cleared on a hole date only where its
            # value is one no evidence row carries (never a real value).
            clear = [symbol for symbol in session.plan.clear.get(value, [])
                     if (symbol, value) in session.mismatched]
            if not rows and not clear:
                continue
            try:
                hole_fills[str(value)] = await dependencies.run_database(
                    functools.partial(_persist_date, dependencies.database, value, rows, clear,
                                      session.available_at),
                    timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
            except Exception as error:  # noqa: BLE001 - a hole is retried next run
                hole_fills[str(value)] = {"error": dependencies.safe_error_detail(str(error), 300)}
    plan["hole_fills"] = hole_fills

    counts = {name: sum(1 for item in results if item["outcome"] == name)
              for name in ("completed", "skipped", "failed")}
    plan["results"] = results
    plan["completed_dates"] = counts["completed"]
    plan["skipped_dates"] = counts["skipped"]
    plan["failed_dates"] = counts["failed"]
    plan["status"] = (
        FAILED_STATUS if counts["failed"] else
        "skipped" if counts["skipped"] and not counts["completed"] else
        "completed" if counts["completed"] else
        # No pending date, only holes: filled (or nothing to fill) unless the
        # longhu fetch itself failed.
        FAILED_STATUS if session.failure is not None else
        "completed" if hole_fills else "unchanged"
    )
    if session.failure is not None:
        plan["reason"] = session.failure
    if session.plan is not None:
        summary = derivation.plan_summary(session.plan, detail_limit=10)
        plan["derivation"] = {key: summary[key] for key in (
            "symbols", "held_symbols", "anchors_missing", "new_listings", "actions_by_basis",
            "flags", "action_samples", "stored_factor_comparisons",
            "stored_factor_disagreements", "longhu_fetch_errors")}
    if session.plan is not None or session.failure is not None:
        await dependencies.run_database(functools.partial(
            _record_fetch_run, dependencies.database, session, plan))
    return plan


#: Dates in a window that are complete by coverage but still carry a NULL
#: factor on an A-share bar whose symbol HAS a promotable factor before it --
#: the hole a failed fetch or an unresolved step leaves behind.
HOLE_DATES_SQL = f"""SELECT DISTINCT bar.trading_date FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.adj_factor IS NULL
   AND bar.quality_status IN ('fresh','partial')
   AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
   AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)
   AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
                WHERE factor.symbol=bar.symbol AND factor.trading_date<bar.trading_date
                  AND factor.adj_factor > 0 AND {REAL_FACTOR_PREDICATE_SQL})
 ORDER BY 1"""


def _value_mismatch_keys(database: Any, start_date: date, end_date: date) -> list[tuple[str, date]]:
    with database.transaction() as connection:
        return [(row["symbol"], row["trading_date"]) for row in connection.execute(
            factor_value_mismatch_sql("canonical_bars_daily"), (start_date, end_date)).fetchall()]


def _hole_dates(database: Any, start_date: date, end_date: date) -> list[date]:
    with database.transaction() as connection:
        return [row["trading_date"] for row in connection.execute(
            HOLE_DATES_SQL, (start_date, end_date)).fetchall()]


def _read_window(database: Any, from_date: date, to_date: date) -> derivation.WindowInputs:
    with database.transaction() as connection:
        return derivation.read_window(connection, from_date, to_date)


def _persist_date(database: Any, trade_date: date, rows: list[Any], clear: list[str],
                  available_at: datetime) -> dict[str, int]:
    """One trading date, one transaction (the ordering rule of the repair)."""
    with database.transaction() as connection:
        return derivation.persist_factor_date(
            connection, trade_date, rows, available_at=available_at, clear_symbols=clear)


def _record_fetch_run(database: Any, session: FactorLaneSession, result: dict[str, Any]) -> None:
    """One ``quant.fetch_runs`` receipt per lane run, under the REAL provider name."""
    stamp = session.available_at.isoformat()
    request_key = hashlib.sha256(json.dumps(
        {"capability": "adj_factor", "provider": derivation.PROVIDER_KEY,
         "dates": [str(value) for value in session.window], "at": stamp},
        sort_keys=True).encode()).hexdigest()
    rows = sum(int(item.get("rows") or 0) for item in result.get("results") or []
               if isinstance(item, dict))
    status = ("failed" if session.failure is not None or result.get("status") == FAILED_STATUS
              else "completed" if result.get("status") == "completed" else "partial")
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.fetch_runs(provider_key,capability,trade_date,request_key,status,
                   attempt_count,row_count,started_at,finished_at,error_class,error_message,metadata)
               VALUES(%s,'adj_factor',%s,%s,%s,1,%s,%s,now(),%s,%s,%s)
               ON CONFLICT(request_key) DO NOTHING""",
            (derivation.PROVIDER_KEY, session.window[-1], request_key, status, rows,
             session.available_at,
             "LonghuFactorDerivationError" if session.failure else None,
             session.failure,
             Json({"source": derivation.SOURCE, "method": derivation.METHOD_VERSION,
                   "dates": [str(value) for value in session.work],
                   "longhu_fetch_errors": len(session.fetch_errors),
                   "writes": session.written})))


# --------------------------------------------------------------------------
# One-time repair (idempotent) and validation
# --------------------------------------------------------------------------

#: How far back the repair looks for a damaged date when no window is given.
REPAIR_LOOKBACK_SESSIONS = 60
#: A date is damaged when more of its A-share bars than this lack a factor
#: (the healthy baseline is a handful of suspended/unfactored symbols a day),
#: or when any bar carries a factor that no promotable evidence supports.
REPAIR_NULL_SHARE = 0.05

#: Data-derived repair window: every recent open session whose A-share bars
#: are damaged, plus every date still carrying un-annotated placeholder
#: evidence.  No literal date anywhere.
REPAIR_DAMAGED_DATES_SQL = f"""
WITH recent AS (
    SELECT DISTINCT calendar_date AS trading_date FROM quant.market_trade_calendar
     WHERE is_open AND calendar_date <= %(today)s
     ORDER BY 1 DESC LIMIT %(sessions)s
), per_date AS (
    SELECT bar.trading_date,
           count(*)::bigint AS bars,
           count(*) FILTER (WHERE bar.adj_factor IS NULL)::bigint AS null_factor,
           count(*) FILTER (WHERE bar.adj_factor IS NOT NULL
               AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors evidence
                            WHERE evidence.symbol=bar.symbol AND evidence.trading_date=bar.trading_date)
               AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
                            WHERE factor.symbol=bar.symbol AND factor.trading_date=bar.trading_date
                              AND {REAL_FACTOR_PREDICATE_SQL}))::bigint AS unsupported_factor
      FROM quant.canonical_bars_daily bar JOIN recent USING (trading_date)
     WHERE bar.quality_status IN ('fresh','partial')
       AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
     GROUP BY bar.trading_date
)
SELECT trading_date, bars, null_factor, unsupported_factor FROM per_date
 WHERE null_factor > %(null_share)s * bars OR unsupported_factor > 0
UNION
SELECT DISTINCT placeholder.trading_date, NULL::bigint, NULL::bigint, NULL::bigint
  FROM quant.daily_adjustment_factors placeholder
 WHERE placeholder.provider='longhuvip_composite'
   AND placeholder.raw->>'factor_semantics'='same_day_identity_only'
   AND placeholder.raw->>'superseded_at' IS NULL
 ORDER BY 1"""

LATEST_SETTLED_DATE_SQL = f"""SELECT max(bar.trading_date) AS latest FROM quant.canonical_bars_daily bar
 WHERE bar.quality_status IN ('fresh','partial') AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
   AND bar.trading_date <= %s
   AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)"""

WINDOW_SESSIONS_SQL = f"""SELECT DISTINCT bar.trading_date FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.quality_status IN ('fresh','partial')
   AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
   AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)
 ORDER BY 1"""

#: Bars in the window that the release guard counts today.
WINDOW_LEAKS_SQL = f"""SELECT bar.symbol, bar.trading_date FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.adj_factor IS NOT NULL
   AND EXISTS (SELECT 1 FROM quant.daily_adjustment_factors evidence
                WHERE evidence.symbol=bar.symbol AND evidence.trading_date=bar.trading_date)
   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
                WHERE factor.symbol=bar.symbol AND factor.trading_date=bar.trading_date
                  AND {REAL_FACTOR_PREDICATE_SQL})"""

WINDOW_NULLS_SQL = f"""SELECT bar.symbol, bar.trading_date FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.adj_factor IS NULL
   AND bar.quality_status IN ('fresh','partial') AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
   AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)"""

#: The value check the identity-leak guard cannot make: an A-share bar whose
#: factor equals NO promotable evidence row of its own date.  The guard only
#: asks whether promotable evidence EXISTS, so a placeholder 1 sitting next to
#: a real tushare row (about 25k bars on 2026-09-01/07/09/10/17) is invisible
#: to it; this query sees it.  Must be 0 over the repaired window.
FACTOR_VALUE_MISMATCH_TEMPLATE = f"""SELECT bar.symbol, bar.trading_date FROM quant.{{table}} bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.adj_factor IS NOT NULL
   AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
   AND NOT EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
                    WHERE factor.symbol=bar.symbol AND factor.trading_date=bar.trading_date
                      AND factor.adj_factor = bar.adj_factor
                      AND {REAL_FACTOR_PREDICATE_SQL})"""


def factor_value_mismatch_sql(table: str = "canonical_bars_daily") -> str:
    """Rows of one pinned bar table whose factor no evidence row carries."""
    if table not in GUARDED_BAR_TABLES:
        raise ValueError(f"table must be one of {GUARDED_BAR_TABLES}; got {table!r}")
    # replace, not format: the symbol pattern carries literal braces.
    return FACTOR_VALUE_MISMATCH_TEMPLATE.replace("{table}", table)


def factor_value_mismatches(connection: Any, start_date: date, end_date: date) -> dict[str, int]:
    return {table: len(connection.execute(
                factor_value_mismatch_sql(table), (start_date, end_date)).fetchall())
            for table in GUARDED_BAR_TABLES}


READBACK_SQL = f"""SELECT bar.trading_date, count(*)::bigint AS bars,
       count(*) FILTER (WHERE bar.adj_factor IS NULL)::bigint AS null_factor,
       count(*) FILTER (WHERE bar.adj_factor = 1)::bigint AS eq1,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM quant.daily_adjustment_factors factor
            WHERE factor.symbol=bar.symbol AND factor.trading_date=bar.trading_date
              AND factor.provider=%s))::bigint AS derived
  FROM quant.canonical_bars_daily bar
 WHERE bar.trading_date BETWEEN %s AND %s AND bar.quality_status IN ('fresh','partial')
   AND bar.symbol ~ '{derivation.A_SHARE_SQL_PATTERN}'
 GROUP BY 1 ORDER BY 1"""


def _count_by_date(keys: list[tuple[str, date]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _symbol, value in keys:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))


def repair_window(
    connection: Any, *, today: date, from_date: date | None = None, to_date: date | None = None,
    lookback_sessions: int = REPAIR_LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Resolve the repair window from the data (explicit bounds win)."""
    damaged = connection.execute(REPAIR_DAMAGED_DATES_SQL, {
        "today": today, "sessions": int(lookback_sessions), "null_share": REPAIR_NULL_SHARE,
    }).fetchall()
    latest = (connection.execute(LATEST_SETTLED_DATE_SQL, (today,)).fetchone() or {}).get("latest")
    derived_from = min((row["trading_date"] for row in damaged), default=None)
    start = from_date or derived_from
    end = to_date or latest
    sessions = [] if start is None or end is None else [
        row["trading_date"] for row in connection.execute(WINDOW_SESSIONS_SQL, (start, end)).fetchall()]
    return {
        "from_date": start, "to_date": end, "derived_from_date": derived_from,
        "latest_settled_date": latest, "sessions": sessions,
        "damaged_dates": [{"trading_date": str(row["trading_date"]),
                           "bars": row["bars"], "null_factor": row["null_factor"],
                           "unsupported_factor": row["unsupported_factor"]} for row in damaged],
    }


def guard_counts(connection: Any) -> dict[str, int]:
    return {table: int(connection.execute(identity_factor_leak_sql(table)).fetchone()["identity_leaks"])
            for table in GUARDED_BAR_TABLES}


def repair_projection(connection: Any, plan: derivation.FactorPlan) -> dict[str, Any]:
    """What the guard and the NULL count WILL be once the plan is applied."""
    written = {(row.symbol, value) for value, rows in plan.rows.items() for row in rows}
    cleared = {(symbol, value) for value, symbols in plan.clear.items() for symbol in symbols}
    leaks = [(row["symbol"], row["trading_date"]) for row in connection.execute(
        WINDOW_LEAKS_SQL, (plan.from_date, plan.to_date)).fetchall()]
    remaining_leaks = [key for key in leaks if key not in written and key not in cleared]
    guard = guard_counts(connection)
    nulls = [(row["symbol"], row["trading_date"]) for row in connection.execute(
        WINDOW_NULLS_SQL, (plan.from_date, plan.to_date)).fetchall()]
    placeholder_nulls = [key for key in leaks if key in cleared and key not in written]
    left_null: dict[str, dict[str, Any]] = {}
    for symbol, value in [*[key for key in nulls if key not in written], *placeholder_nulls]:
        entry = left_null.setdefault(symbol, {
            "reason": plan.held.get(symbol) or (
                "unresolved_step_across_bar_gap" if symbol in plan.truncated
                else "longhu_fetch_failed" if symbol in plan.fetch_errors
                else "no_derivation_for_this_bar"),
            "dates": []})
        entry["dates"].append(str(value))
    for entry in left_null.values():
        entry["dates"] = sorted(set(entry["dates"]))
    outside = guard["canonical_bars_daily"] - len(leaks)
    mismatches = [(row["symbol"], row["trading_date"]) for row in connection.execute(
        factor_value_mismatch_sql("canonical_bars_daily"), (plan.from_date, plan.to_date)).fetchall()]
    # A planned row sets the value from its evidence; a cleared bar keeps a
    # value only if an equal evidence row exists, i.e. was never a mismatch.
    remaining_mismatches = [key for key in mismatches if key not in written and key not in cleared]
    return {
        "value_mismatches_now": len(mismatches),
        "value_mismatches_after_apply_projected": len(remaining_mismatches),
        "value_mismatch_dates_now": _count_by_date(mismatches),
        "guard_now": guard,
        "guard_leaks_in_window": len(leaks),
        "guard_leaks_outside_window": outside,
        "guard_after_apply_projected": {
            "canonical_bars_daily": outside + len(remaining_leaks),
            # The market table gets the same values and never carried placeholders.
            "market_bars_daily": guard["market_bars_daily"]},
        "null_bars_now": len(nulls),
        "null_bars_after_apply": sum(len(entry["dates"]) for entry in left_null.values()),
        "symbols_left_null": dict(sorted(left_null.items())),
    }


async def repair(
    dependencies: AdjustmentFactorMaintenanceDependencies, *,
    apply: bool = False, from_date: date | None = None, to_date: date | None = None,
    today: date | None = None, lookback_sessions: int = REPAIR_LOOKBACK_SESSIONS,
) -> dict[str, Any]:
    """Backfill real factors over the damaged window; idempotent, dry run by default.

    Every symbol continues from its last stored promotable factor BEFORE the
    window (tushare-era in practice) through the latest settled bar; stored
    tushare factors inside the window (the 08-27 hole's evidence, the
    09-02/09-03 bars, the 09-07/09-09/09-10/09-17 cross-sections) are
    checkpoints -- compared with the derivation, reported when they disagree,
    kept as they are.  A row marked ``superseded_at`` is never a checkpoint or
    an anchor; an operator's ``manual_*`` derived row is one and is never
    recomputed.  ``apply`` writes one transaction per trading date
    (derived rows + both bar tables + placeholder annotation + ledger clear),
    then re-runs the guard and reads back every date.  Without ``apply`` not a
    single write is issued: the dry run is safe on a read-only connection.
    """
    if apply and getattr(dependencies, 'control', None) is not None:
        return await dependencies.control(dependencies, 'repair', repair,
            dict(apply=apply, from_date=from_date, to_date=to_date, today=today,
                 lookback_sessions=lookback_sessions))
    end_date = today or china_today()
    database = dependencies.database
    window = await dependencies.run_database(
        functools.partial(_repair_window, database, end_date, from_date, to_date, lookback_sessions),
        timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run", "provider": derivation.PROVIDER_KEY,
        "method": derivation.METHOD_VERSION,
        "window": {key: (str(value) if isinstance(value, date) else value)
                   for key, value in window.items() if key != "sessions"},
    }
    if window["from_date"] is None or not window["sessions"]:
        report["status"] = "unchanged"
        report["reason"] = "no damaged date and no un-annotated placeholder evidence"
        return report
    inputs = await dependencies.run_database(
        functools.partial(_read_window, database, window["from_date"], window["to_date"]),
        timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    sessions = derivation.sessions_to_fetch(inputs, end_date)
    source = dependencies.longhu_source()
    longhu, errors = await dependencies.run_public(
        derivation.fetch_longhu_evidence, source, sessions,
        timeout_seconds=LONGHU_FETCH_TIMEOUT_SECONDS, workers=LONGHU_FETCH_WORKERS)
    if sessions and len(errors) > MAX_FETCH_FAILURE_RATIO * len(sessions):
        report["status"] = FAILED_STATUS
        report["reason"] = f"longhu kline failed for {len(errors)} of {len(sessions)} symbols"
        return report
    plan = derivation.build_plan(inputs, longhu, write_dates=window["sessions"],
                                 fetch_errors=errors, rederive_derived=True)
    report["plan"] = derivation.plan_summary(plan)
    report["projection"] = await dependencies.run_database(
        functools.partial(_repair_projection, database, plan), timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    if not apply:
        report["status"] = "planned"
        return report
    available_at = dependencies.now()
    applied: dict[str, dict[str, int]] = {}
    for value in plan.write_dates:
        applied[str(value)] = await dependencies.run_database(
            functools.partial(_apply_repair_date, database, value, plan.rows.get(value, []),
                              list(plan.clear.get(value, [])), available_at),
            timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    report["applied"] = applied
    report["after"] = await dependencies.run_database(
        functools.partial(_repair_readback, database, plan.from_date, plan.to_date),
        timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    guard = report["after"]["guard"]
    mismatches = report["after"].get("value_mismatches") or {}
    failures = []
    if any(guard.values()):
        failures.append(f"release guard is not 0 after the repair: {guard}")
    if any(mismatches.values()):
        failures.append(f"bars whose factor equals no evidence row remain in the window: {mismatches}")
    report["status"] = FAILED_STATUS if failures else "completed"
    if failures:
        report["reason"] = "; ".join(failures)
    return report


def _repair_window(database: Any, today: date, from_date: date | None, to_date: date | None,
                   lookback_sessions: int) -> dict[str, Any]:
    with database.transaction() as connection:
        return repair_window(connection, today=today, from_date=from_date, to_date=to_date,
                             lookback_sessions=lookback_sessions)


def _repair_projection(database: Any, plan: derivation.FactorPlan) -> dict[str, Any]:
    with database.transaction() as connection:
        return repair_projection(connection, plan)


def _apply_repair_date(database: Any, trade_date: date, rows: list[Any], clear: list[str],
                       available_at: datetime) -> dict[str, int]:
    with database.transaction() as connection:
        counts = derivation.persist_factor_date(
            connection, trade_date, rows, available_at=available_at, clear_symbols=clear)
        clear_blocked_date(connection, trade_date)
        return counts


def _repair_readback(database: Any, from_date: date, to_date: date) -> dict[str, Any]:
    with database.transaction() as connection:
        rows = connection.execute(READBACK_SQL, (derivation.PROVIDER_KEY, from_date, to_date)).fetchall()
        return {"guard": guard_counts(connection),
                "value_mismatches": factor_value_mismatches(connection, from_date, to_date),
                "dates": [{key: (str(value) if isinstance(value, date) else int(value))
                           for key, value in dict(row).items()} for row in rows]}


async def validate(
    dependencies: AdjustmentFactorMaintenanceDependencies, *,
    from_date: date, to_date: date, today: date | None = None,
) -> dict[str, Any]:
    """Read-only: re-derive a stored tushare period and score the method."""
    end_date = today or china_today()

    def read() -> tuple[Any, Any]:
        with dependencies.database.transaction() as connection:
            return derivation.read_validation_inputs(connection, from_date, to_date)

    bars, truth = await dependencies.run_database(read, timeout_seconds=FACTOR_DB_TIMEOUT_SECONDS)
    symbols = sorted(symbol for symbol in bars if symbol in truth)
    # Sessions from the period start to today, with slack; one call per symbol.
    span = min(derivation.MAX_SESSIONS_PER_CALL, int((end_date - from_date).days * 5 / 7) + 20)
    source = dependencies.longhu_source()
    longhu, errors = await dependencies.run_public(
        derivation.fetch_longhu_evidence, source, {symbol: span for symbol in symbols},
        timeout_seconds=LONGHU_FETCH_TIMEOUT_SECONDS, workers=LONGHU_FETCH_WORKERS)
    report = derivation.validate(bars, truth, longhu)
    return {"from_date": str(from_date), "to_date": str(to_date), "symbols": len(symbols),
            "longhu_fetch_errors": len(errors), "method": derivation.METHOD_VERSION, **report}


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


#: Per-date coverage for the read-only report: how many settled bars the date
#: has, and how many of them a REAL cumulative factor exists for.  Deliberately
#: the same ``REAL_FACTOR_PREDICATE_SQL`` the work list uses, so the report can
#: never disagree with the lane about what "covered" means.
STATUS_DATES_SQL = f"""WITH settled AS (
       SELECT bar.trading_date, bar.symbol FROM quant.canonical_bars_daily bar
        WHERE bar.trading_date BETWEEN %s AND %s
          AND bar.quality_status IN ('fresh','partial')
          AND EXISTS (SELECT 1 FROM quant.market_trade_calendar calendar
                       WHERE calendar.calendar_date=bar.trading_date AND calendar.is_open)
   ), factored AS (
       SELECT DISTINCT factor.trading_date, factor.symbol
         FROM quant.daily_adjustment_factors factor
        WHERE factor.trading_date BETWEEN %s AND %s
          AND {REAL_FACTOR_PREDICATE_SQL}
   ) SELECT settled.trading_date,
            count(*)::bigint AS daily_rows,
            count(*) FILTER (WHERE factored.symbol IS NOT NULL)::bigint AS adjustment_rows
       FROM settled LEFT JOIN factored USING (trading_date, symbol)
      GROUP BY settled.trading_date ORDER BY settled.trading_date"""

#: The factor route's own fetch receipts.  ``last_error`` is deliberately NOT
#: selected: it is provider text of unbounded shape and this report is printed
#: on an operator console and pasted into tickets.  The shape of the failure
#: (``error_class``) answers the operational question without carrying a URL or
#: a token into a log.
FACTOR_FETCH_RUNS_SQL = """SELECT provider_key,status,row_count,trade_date,attempt_count,
            error_class,started_at,finished_at,created_at
       FROM quant.fetch_runs WHERE capability='adj_factor'
      ORDER BY coalesce(finished_at,started_at,created_at) DESC LIMIT %s"""

FACTOR_PROVIDER_HEALTH_SQL = """SELECT provider_key,market,consecutive_failures,circuit_open_until,
            last_success_at,last_failure_at,last_row_count,last_latency_ms
       FROM quant.provider_health WHERE capability='adj_factor' ORDER BY provider_key"""

#: The durable receipt the non-gating post-close stage leaves behind.  The
#: ``run_key`` shape is ``<receipt version>:<stage>:<trade date>``
#: (``post_close_refresh.record_stage_with_receipt``); the pattern is passed as
#: a parameter rather than inlined so no ``%`` escaping is involved.
POST_CLOSE_STAGE_TASK_KEY = "post_close_refresh.stage"
POST_CLOSE_FACTOR_RECEIPT_PATTERN = "%:adjustment_factors:%"
POST_CLOSE_FACTOR_RECEIPT_SQL = """SELECT run_key,as_of_date,status,methodology_version,
            output_summary,started_at,finished_at,updated_at
       FROM quant.automation_runs WHERE task_key=%s AND run_key LIKE %s
      ORDER BY as_of_date DESC NULLS LAST, updated_at DESC LIMIT 1"""

#: The fields of a post-close receipt worth reprinting.  The whole
#: ``output_summary`` carries the lane's full per-date results and would bury
#: the answer; these are the ones that say what the stage decided.
POST_CLOSE_RECEIPT_FIELDS = (
    "status", "lane_status", "retryable", "unrepaired_dates", "lookback_days", "reason")


def _text(value: Any) -> Any:
    """Stringify dates and timestamps; leave JSON-native values alone."""
    return value if value is None or isinstance(value, (bool, int, float, str)) else str(value)


def status_report(
    connection: Any, start_date: date, end_date: date,
    *, minimum_ratio: float = PENDING_COVERAGE_RATIO, fetch_runs: int = 5,
    capability: Any = None,
) -> dict[str, Any]:
    """Read-only answer to "where does the factor lane actually stand?".

    Every number here is read through the same helpers the lane itself uses --
    :data:`REAL_FACTOR_PREDICATE_SQL` for coverage,
    :func:`pending_and_retired_dates_between` for the work list and the
    retirement ledger, :func:`identity_factor_leak_sql` for the release guard
    -- so an operator reading this report and the lane making decisions can
    never be looking at two different definitions of the same word.

    Nothing is written.  The identity-leak guard is deliberately UNWINDOWED:
    it is a release gate that must return 0 over the whole table, and a
    windowed version of it would pass while pollution sits one day outside.
    """
    coverage = connection.execute(
        STATUS_DATES_SQL, (start_date, end_date, start_date, end_date)).fetchall()
    pending, retired = pending_and_retired_dates_between(
        connection, start_date, end_date, minimum_ratio=minimum_ratio)
    pending_set = set(pending)
    dates: list[dict[str, Any]] = []
    for row in coverage:
        trading_date = row["trading_date"]
        entry: dict[str, Any] = {
            "trading_date": str(trading_date),
            "daily_rows": int(row["daily_rows"] or 0),
            "adjustment_rows": int(row["adjustment_rows"] or 0),
            "pending": trading_date in pending_set,
            "retired": None,
        }
        entry["coverage_ratio"] = round(
            entry["adjustment_rows"] / entry["daily_rows"], 6) if entry["daily_rows"] else None
        ledger = retired.get(trading_date)
        if ledger is not None:
            entry["retired"] = {
                "run_key": ledger["run_key"], "reason": str(ledger["reason"]),
                "blocked_days": list(ledger["blocked_days"]),
                "consecutive_blocked_runs": int(ledger["consecutive_blocked_runs"]),
                "retired_at": _text(ledger.get("retired_at")),
            }
        dates.append(entry)
    leaks = {
        table: int(connection.execute(identity_factor_leak_sql(table)).fetchone()["identity_leaks"])
        for table in GUARDED_BAR_TABLES
    }
    mismatches = factor_value_mismatches(connection, start_date, end_date)
    runs = [
        {key: _text(value) for key, value in dict(row).items()}
        for row in connection.execute(FACTOR_FETCH_RUNS_SQL, (int(fetch_runs),)).fetchall()
    ]
    health = []
    for row in connection.execute(FACTOR_PROVIDER_HEALTH_SQL).fetchall():
        item = {key: _text(value) for key, value in dict(row).items()}
        item["circuit_open"] = bool(row.get("circuit_open_until"))
        health.append(item)
    receipt_row = connection.execute(
        POST_CLOSE_FACTOR_RECEIPT_SQL,
        (POST_CLOSE_STAGE_TASK_KEY, POST_CLOSE_FACTOR_RECEIPT_PATTERN),
    ).fetchone()
    receipt: dict[str, Any] | None = None
    if receipt_row is not None:
        summary = dict(receipt_row.get("output_summary") or {})
        receipt = {key: _text(receipt_row.get(key)) for key in
                   ("run_key", "as_of_date", "status", "methodology_version",
                    "started_at", "finished_at", "updated_at")}
        receipt["output_summary"] = {
            key: _text(summary[key]) if not isinstance(summary.get(key), (list, dict)) else summary[key]
            for key in POST_CLOSE_RECEIPT_FIELDS if key in summary}
    report: dict[str, Any] = {
        "generated_for": {"start_date": str(start_date), "end_date": str(end_date),
                          "minimum_coverage_ratio": float(minimum_ratio)},
        "dates": dates,
        "pending_dates": [str(value) for value in pending],
        "retired_dates": sorted(str(value) for value in retired),
        "identity_factor_leaks": leaks,
        # Windowed (the guard above is not): bars whose factor equals no
        # promotable evidence row of their date.  0 after a correct repair.
        "factor_value_mismatches": mismatches,
        "factor_fetch_runs": runs,
        "provider_capability": dict(_capability_payload(capability)),
        "provider_health": health,
        "post_close_stage_receipt": receipt,
    }
    report["summary"] = status_summary(report)
    return report


@dataclass(frozen=True)
class FactorRoute:
    """The factor lane's own route, in the shape the status report reads."""

    frequency: str
    status: str
    decision_eligible: bool
    preferred_providers: tuple[str, ...]
    note: str


def longhu_factor_route(configured: bool) -> FactorRoute:
    """Describe the ONE route this lane uses (no tushare registry entry).

    verified only says the licensed longhu source is configured on this
    host; whether factors actually landed is what factor_fetch_runs and
    the per-date coverage in the same report answer.
    """
    return FactorRoute(
        frequency="daily", status="verified" if configured else "unconfigured",
        decision_eligible=True, preferred_providers=(derivation.PROVIDER_KEY,),
        note=(f"derived from {derivation.SOURCE} (CQ record + qfq series) and the bar pre_close; "
              "no tushare call"))


def _capability_payload(capability: Any) -> dict[str, Any]:
    """Describe the declared adj_factor route without importing a registry here.

    The caller passes the registry's answer in, so this module keeps its single
    dependency direction (nothing in ``app`` imports it back) and the report can
    be built in a test without the registry at all.
    """
    if capability is None:
        return {"available": None, "note": "capability registry was not consulted"}
    return {
        "api": "adj_factor",
        "frequency": getattr(capability, "frequency", None),
        "status": getattr(capability, "status", None),
        "decision_eligible": bool(getattr(capability, "decision_eligible", False)),
        "preferred_providers": list(getattr(capability, "preferred_providers", ()) or ()),
        "note": getattr(capability, "note", None),
        # "verified" is the registry saying a real cross-section has landed
        # through this route; anything else is a declaration, not evidence.
        "available": getattr(capability, "status", None) == "verified",
    }


def status_summary(report: dict[str, Any]) -> str:
    """One ASCII line an operator can read without opening the JSON.

    Pure: it reads the assembled report and nothing else, so the sentence can
    be pinned by a unit test and cannot drift from the numbers above it.
    """
    dates = list(report.get("dates") or [])
    pending = len(report.get("pending_dates") or [])
    retired = len(report.get("retired_dates") or [])
    complete = sum(1 for item in dates
                   if not item.get("pending") and item.get("retired") is None)
    leaks = sum(int(value) for value in (report.get("identity_factor_leaks") or {}).values())
    window = report.get("generated_for") or {}
    parts = [
        f"{window.get('start_date')}..{window.get('end_date')}: {len(dates)} settled date(s), "
        f"{complete} complete, {pending} pending, {retired} retired",
        f"identity factor leaks {leaks}",
    ]
    if "factor_value_mismatches" in report:
        parts.append("factor value mismatches "
                     f"{sum(int(value) for value in (report.get('factor_value_mismatches') or {}).values())}")
    runs = list(report.get("factor_fetch_runs") or [])
    if runs:
        last = runs[0]
        parts.append(
            f"last fetch {last.get('provider_key')} {last.get('status')} "
            f"rows={last.get('row_count')} at {last.get('finished_at') or last.get('started_at')}")
    else:
        parts.append("no adj_factor fetch run on record")
    capability = report.get("provider_capability") or {}
    if capability.get("available") is not None:
        parts.append(
            f"route {'available' if capability.get('available') else 'unverified'} "
            f"({', '.join(capability.get('preferred_providers') or []) or 'no preferred provider'})")
    receipt = report.get("post_close_stage_receipt")
    if receipt:
        parts.append(f"post-close receipt {receipt.get('status')} for {receipt.get('as_of_date')}")
    else:
        parts.append("no post-close factor-stage receipt")
    return "; ".join(parts)


async def status(
    dependencies: AdjustmentFactorMaintenanceDependencies,
    *, lookback_days: int = 30, today: date | None = None, capability: Any = None,
) -> dict[str, Any]:
    """Read-only status of the factor lane over one lookback window."""
    if lookback_days < 0:
        raise ValueError("lookback_days must not be negative")
    end_date = today or china_today()
    start_date = end_date - timedelta(days=lookback_days)
    report = await dependencies.run_database(functools.partial(
        _status_report, dependencies.database, start_date, end_date, capability))
    return {"lookback_days": int(lookback_days), **report}


def _status_report(database: Any, start_date: date, end_date: date, capability: Any) -> dict[str, Any]:
    with database.transaction() as connection:
        return status_report(connection, start_date, end_date, capability=capability)


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
    "POST_CLOSE_FACTOR_RECEIPT_PATTERN", "POST_CLOSE_FACTOR_RECEIPT_SQL",
    "POST_CLOSE_NON_GATING_REASON", "POST_CLOSE_RECEIPT_FIELDS",
    "POST_CLOSE_STAGE_TASK_KEY", "POST_CLOSE_TERMINAL_LANE_STATUSES",
    "REAL_FACTOR_PREDICATE_SQL", "RETIRED_DATES_SQL", "RETIRED_DATE_DEFAULT_REASON",
    "STATUS_DATES_SQL", "SUCCESS_STATUSES",
    "blocked_date_run_key", "blocked_ledger_day", "china_today", "clear_blocked_date",
    "identity_factor_leak_sql",
    "pending_and_retired_dates_between", "pending_dates", "pending_dates_between",
    "post_close_lookback_days", "post_close_lookback_days_from_calendar",
    "post_close_stage_receipt", "post_close_sync", "record_blocked_date",
    "retired_date_details", "retired_date_details_from_rows", "retired_dates",
    "status", "status_report", "status_summary", "sync",
    "FactorLaneSession", "LONGHU_FETCH_WORKERS", "MAX_FETCH_FAILURE_RATIO",
    "REPAIR_DAMAGED_DATES_SQL", "REPAIR_LOOKBACK_SESSIONS", "guard_counts", "repair",
    "repair_factor_date", "repair_projection", "repair_window", "validate",
    "FactorRoute", "HOLE_DATES_SQL", "longhu_factor_route",
    "FACTOR_VALUE_MISMATCH_TEMPLATE", "factor_value_mismatch_sql", "factor_value_mismatches",
]
