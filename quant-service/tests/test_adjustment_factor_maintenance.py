"""Coverage for the out-of-band cumulative adjustment-factor repair lane.

The job exists because the settled cross-section and the corporate-action
factor now come from different providers: a trading date can land with
complete bars and limits while its factors are still missing.  It runs in the
04:00-08:00 maintenance window, never inside the post-close pipeline.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import os
import unittest

import app.adjustment_factor_maintenance as module
from app.adjustment_factor_maintenance import (
    AdjustmentFactorMaintenanceDependencies,
    BLOCKED_DATE_TASK_KEY,
    CHINA,
    MAX_CONSECUTIVE_BLOCKED_RUNS,
    PENDING_COVERAGE_RATIO,
    POST_CLOSE_LOOKBACK_DAYS,
    POST_CLOSE_LOOKBACK_SESSIONS,
    POST_CLOSE_TERMINAL_LANE_STATUSES,
    blocked_date_run_key,
    blocked_ledger_day,
    china_today,
    clear_blocked_date,
    pending_and_retired_dates_between,
    pending_dates,
    pending_dates_between,
    post_close_lookback_days,
    post_close_lookback_days_from_calendar,
    post_close_stage_receipt,
    post_close_sync,
    record_blocked_date,
    retired_date_details,
    retired_dates,
    sync,
)
from app.full_market_daily_controls_sync import COVERAGE_BLOCK_REASON, PROVIDER_BLOCK_REASON
from app.post_close_refresh import record_stage_with_receipt


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _BlockedDateLedger:
    """An in-memory twin of ``_RECORD_BLOCKED_DATE_SQL``'s (date, day) key.

    The statement itself is what production runs and it is proved on real
    PostgreSQL by :class:`BlockedDateLedgerPostgresTests`; this model exists so
    the LANE can be driven through a dozen invocations an evening for five
    evenings -- the shape that retired a date in one evening before the key was
    fixed -- without a database.  ``day`` is the ledger day "now" belongs to and
    the test moves it, which is exactly what a new evening does.
    """

    def __init__(self, day=date(2026, 9, 15)):
        self.day = day
        self.rows: dict[str, dict] = {}
        self.receipts: list[tuple] = []

    def record(self, run_key: str, as_of_date, reason: str) -> dict:
        row = self.rows.setdefault(run_key, {
            "as_of_date": as_of_date, "run_key": run_key,
            "output_summary": {"consecutive_blocked_runs": 0, "blocked_days": []}})
        summary = row["output_summary"]
        if str(self.day) not in summary["blocked_days"]:
            summary["blocked_days"] = [*summary["blocked_days"], str(self.day)]
            summary["consecutive_blocked_runs"] += 1
        summary["reason"] = reason
        summary["last_blocked_at"] = f"{self.day} 20:00:00"
        return dict(summary)

    def retired(self, run_keys) -> list[dict]:
        return [row for key, row in self.rows.items()
                if key in set(run_keys)
                and row["output_summary"]["consecutive_blocked_runs"] >= MAX_CONSECUTIVE_BLOCKED_RUNS]

    def mark_receipt_written(self, run_key: str) -> None:
        self.rows[run_key]["output_summary"]["retirement_receipt_written"] = True

    def clear(self, run_key: str) -> None:
        if run_key in self.rows:
            self.rows[run_key]["output_summary"].update(
                {"consecutive_blocked_runs": 0, "blocked_days": [],
                 "retirement_receipt_written": False})


class _Connection:
    """A connection that answers per statement rather than one canned result."""

    def __init__(self, rows, ledger_rows=None, blocked_summary=None, ledger=None,
                 sessions=None):
        self.rows = rows
        self.ledger_rows = ledger_rows or []
        self.blocked_summary = blocked_summary or {"consecutive_blocked_runs": 1}
        self.ledger = ledger
        #: What the trade calendar answers the post-close window query with.
        self.sessions = sessions
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.calls.append((text, params))
        if "quant.market_trade_calendar" in text and "oldest_session" in text:
            return _Result([self.sessions if self.sessions is not None
                            else {"oldest_session": None, "sessions": 0}])
        if "quant.automation_runs" in text and text.startswith("SELECT as_of_date"):
            if self.ledger is not None:
                return _Result(self.ledger.retired(params[1]))
            return _Result(self.ledger_rows)
        if "INSERT INTO quant.automation_runs" in text:
            if self.ledger is not None:
                return _Result([{"output_summary": self.ledger.record(params[1], params[2], params[5])}])
            return _Result([{"output_summary": dict(self.blocked_summary)}])
        if self.ledger is not None and "data_quality_issues" in text and text.startswith("INSERT"):
            self.ledger.receipts.append(params)
        if self.ledger is not None and "'retirement_receipt_written', true" in text:
            self.ledger.mark_receipt_written(params[0])
        if self.ledger is not None and "'cleared_at'" in text:
            self.ledger.clear(params[0])
        if "quant.automation_runs" in text or "data_quality_issues" in text:
            return _Result([])
        return _Result(self.rows)


class _Database:
    def __init__(self, rows, ledger_rows=None, blocked_summary=None, ledger=None, sessions=None):
        self.connection = _Connection(rows, ledger_rows, blocked_summary, ledger, sessions)

    def transaction(self):
        class Context:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_args): return False
        return Context(self.connection)


async def _run_database(action, *args, **_kwargs):
    return action(*args)


def _no_longhu():
    raise AssertionError("these tests replace the per-date repair; nothing may reach longhu")


async def _no_public_call(*_args, **_kwargs):
    raise AssertionError("these tests replace the per-date repair; nothing may fetch")


def _per_date(fake):
    """Adapt a per-date fake to :func:`repair_factor_date`'s ``(session, date)``.

    The lane's per-date unit used to be the tushare controls sync; it is now
    the longhu derivation.  The fakes below describe per-date OUTCOMES (the
    contract the ledger and the receipt depend on), so they are reused as-is.
    """
    import inspect

    async def repair(_session, trade_date):
        result = fake(trade_date, apis=("adj_factor",))
        if inspect.isawaitable(result):
            result = await result
        return result

    return repair


class PendingDatesTests(unittest.TestCase):
    def test_pending_dates_use_the_readiness_coverage_ratio_and_a_bounded_window(self):
        database = _Database([{"trading_date": date(2026, 9, 17)},
                              {"trading_date": date(2026, 9, 18)}])
        result = pending_dates(database, lookback_days=30, today=date(2026, 9, 19))
        self.assertEqual(result, [date(2026, 9, 17), date(2026, 9, 18)])
        sql, params = database.connection.calls[0]
        self.assertEqual(params, (date(2026, 8, 20), date(2026, 9, 19),
                                  date(2026, 8, 20), date(2026, 9, 19), PENDING_COVERAGE_RATIO))
        # Settled bars only, and a date counts as pending on *coverage*, not on
        # a single missing symbol.
        self.assertIn("bar.quality_status IN ('fresh','partial')", sql)
        # Only exchange sessions: stray rows on a non-trading date can never
        # get a factor cross-section and would sit on the work list forever.
        self.assertIn("calendar.calendar_date=bar.trading_date AND calendar.is_open", sql)
        self.assertIn("HAVING count(*) FILTER (WHERE factored.symbol IS NOT NULL) < ceil(%s * count(*))", sql)
        # Coverage is asked of the factor table, so a date whose bars still
        # carry identity placeholders is still reported as pending.  The
        # predicate is the positive promotion rule (a tushare row whose
        # semantics are absent or cumulative), not the one placeholder marker,
        # so a vendor that simply omits the marker cannot satisfy it either.
        self.assertIn("factor.provider LIKE 'tushare%%'", sql)  # psycopg literal-percent escape
        self.assertIn("coalesce(factor.raw->>'factor_semantics','') IN ('','corporate_action_cumulative')", sql)

    def test_negative_lookback_is_rejected_rather_than_silently_inverted(self):
        with self.assertRaises(ValueError):
            pending_dates(_Database([]), lookback_days=-1)

    def test_between_helper_takes_an_explicit_window_for_the_readiness_labels(self):
        connection = _Connection([{"trading_date": date(2026, 9, 4)}])
        self.assertEqual(
            pending_dates_between(connection, date(2026, 9, 1), date(2026, 9, 18)),
            [date(2026, 9, 4)])
        self.assertEqual(connection.calls[0][1][:4],
                         (date(2026, 9, 1), date(2026, 9, 18), date(2026, 9, 1), date(2026, 9, 18)))

    def test_a_retired_date_is_not_reported_as_pending(self):
        """"Pending" is a promise that a repair is queued; a retired date is not.

        The readiness labels read this helper directly, so a date the ledger
        retired used to be shown as "queued for the adjustment-factor
        maintenance job" forever, although the job had stopped fetching it.
        """
        connection = _Connection(
            [{"trading_date": date(2026, 9, 4)}, {"trading_date": date(2026, 9, 15)}],
            ledger_rows=[{"as_of_date": date(2026, 9, 15), "run_key": "adjustment-factor-blocked:2026-09-15",
                          "output_summary": {"consecutive_blocked_runs": 5, "reason": "thin cross-section"}}])
        self.assertEqual(
            pending_dates_between(connection, date(2026, 9, 1), date(2026, 9, 18)),
            [date(2026, 9, 4)])
        # The ledger really was consulted, with the documented threshold.
        ledger_call = next(call for call in connection.calls if call[0].startswith("SELECT as_of_date"))
        self.assertEqual(ledger_call[1][0], BLOCKED_DATE_TASK_KEY)
        self.assertEqual(ledger_call[1][2], MAX_CONSECUTIVE_BLOCKED_RUNS)
        # The maintenance job asks for the raw list because it reports the two
        # sets separately in its own receipt.
        self.assertEqual(
            pending_dates_between(connection, date(2026, 9, 1), date(2026, 9, 18), include_retired=True),
            [date(2026, 9, 4), date(2026, 9, 15)])

    def test_the_split_helper_returns_the_ledger_evidence_for_a_retired_date(self):
        connection = _Connection(
            [{"trading_date": date(2026, 9, 4)}, {"trading_date": date(2026, 9, 15)}],
            ledger_rows=[{"as_of_date": date(2026, 9, 15), "run_key": "adjustment-factor-blocked:2026-09-15",
                          "output_summary": {"consecutive_blocked_runs": 6, "reason": "thin cross-section",
                                             "blocked_days": ["2026-09-15", "2026-09-16"],
                                             "retired_at": "2026-09-18 04:31:00"}}])
        pending, retired = pending_and_retired_dates_between(
            connection, date(2026, 9, 1), date(2026, 9, 18))
        self.assertEqual(pending, [date(2026, 9, 4)])
        self.assertEqual(retired[date(2026, 9, 15)], {
            "run_key": "adjustment-factor-blocked:2026-09-15", "reason": "thin cross-section",
            "consecutive_blocked_runs": 6, "blocked_days": ["2026-09-15", "2026-09-16"],
            "retired_at": "2026-09-18 04:31:00"})

    def test_retired_details_fall_back_to_the_run_key_and_a_default_reason(self):
        connection = _Connection([], ledger_rows=[{"as_of_date": date(2026, 9, 15)}])
        details = retired_date_details(connection, [date(2026, 9, 15)])
        self.assertEqual(details[date(2026, 9, 15)]["run_key"],
                         blocked_date_run_key(date(2026, 9, 15)))
        self.assertTrue(details[date(2026, 9, 15)]["reason"])

    def test_exchange_local_today_is_shanghai(self):
        self.assertIsInstance(china_today(), date)


class SyncTests(unittest.IsolatedAsyncioTestCase):
    def _dependencies(self, database, **overrides):
        base = dict(
            database=database, run_database=_run_database,
            longhu_source=_no_longhu, run_public=_no_public_call,
            safe_error_detail=lambda value, _limit: value,
        )
        base.update(overrides)
        return AdjustmentFactorMaintenanceDependencies(**base)

    async def test_dry_run_issues_no_provider_call_and_no_write(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        def explode(*_args, **_kwargs):
            raise AssertionError("a dry run must not reach the per-date repair")

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(explode)
        try:
            result = await sync(self._dependencies(database), lookback_days=30, dry_run=True,
                                today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["pending_dates"], ["2026-09-17"])
        self.assertEqual(result["results"], [])
        self.assertTrue(result["dry_run"])

    async def test_each_pending_date_runs_an_adj_factor_only_controls_sync(self):
        database = _Database([{"trading_date": date(2026, 9, 17)},
                              {"trading_date": date(2026, 9, 18)}])
        seen: list[tuple[date, tuple[str, ...]]] = []
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **kwargs):
            seen.append((trade_date, kwargs["apis"]))
            return {"status": "completed", "trade_date": str(trade_date)}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed_dates"], 2)
        self.assertEqual(seen, [(date(2026, 9, 17), ("adj_factor",)),
                                (date(2026, 9, 18), ("adj_factor",))])

    async def test_one_failing_date_does_not_end_the_run_but_fails_it(self):
        database = _Database([{"trading_date": date(2026, 9, 17)},
                              {"trading_date": date(2026, 9, 18)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            if trade_date == date(2026, 9, 17):
                raise RuntimeError("provider refused")
            return {"status": "completed", "trade_date": str(trade_date)}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(result["status"], "failed")
        self.assertEqual((result["completed_dates"], result["skipped_dates"], result["failed_dates"]),
                         (1, 0, 1))
        self.assertEqual(result["results"][0]["outcome"], "failed")
        self.assertEqual(result["results"][0]["trade_date"], "2026-09-17")

    async def test_a_coverage_blocked_date_is_skipped_with_its_reason_and_does_not_fail(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "blocked", "trade_date": str(trade_date),
                    "blocked_by": COVERAGE_BLOCK_REASON,
                    "reason": "full-market daily bars are not ready"}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        # The factor lane cannot repair a thin daily cross-section, so this is
        # a report, not a failure: a scheduled task must not alert forever.
        self.assertEqual(result["status"], "skipped")
        self.assertEqual((result["completed_dates"], result["skipped_dates"], result["failed_dates"]),
                         (0, 1, 0))
        self.assertEqual(result["results"][0]["outcome"], "skipped")
        self.assertEqual(result["results"][0]["reason"], "full-market daily bars are not ready")
        self.assertEqual(result["results"][0]["ledger"]["consecutive_blocked_runs"], 1)
        self.assertFalse(result["results"][0]["ledger"]["retired"])

    async def test_an_evenings_repetitions_do_not_retire_a_date_but_five_evenings_do(self):
        """The retirement rule, driven through the real lane, as it really runs.

        The post-close stage repeats every ``RetryIntervalMinutes`` until
        ~22:40, so one coverage-blocked date reaches ``post_close_sync`` about a
        dozen times in an evening.  While the ledger counted invocations, that
        retired the date at repetition 5 of the FIRST evening -- after which the
        lane found no work, returned ``unchanged`` and sealed its receipt, so
        the retry behaviour switched itself off two and a half hours in.
        """
        import app.adjustment_factor_maintenance as module

        ledger = _BlockedDateLedger(day=date(2026, 9, 15))
        database = _Database([{"trading_date": date(2026, 9, 11)}], ledger=ledger)
        dependencies = self._dependencies(database)

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "blocked", "trade_date": str(trade_date),
                    "blocked_by": COVERAGE_BLOCK_REASON,
                    "reason": "full-market daily bars are not ready"}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            for offset in range(MAX_CONSECUTIVE_BLOCKED_RUNS - 1):
                ledger.day = date(2026, 9, 15) + timedelta(days=offset)
                for repetition in range(12):  # one evening of pipeline repetitions
                    result = await post_close_sync(dependencies, today=date(2026, 9, 19))
                    self.assertEqual(result["status"], "blocked", f"evening {offset}/{repetition}")
                    self.assertTrue(result["retryable"])
                    self.assertEqual(result["unrepaired_dates"], ["2026-09-11"])
                    # Every repetition of one evening is the SAME ledger day, so
                    # the counter moves once per evening, not once per run.
                    self.assertEqual(result["results"][0]["ledger"]["consecutive_blocked_runs"],
                                     offset + 1)
            self.assertEqual(ledger.receipts, [], "four evenings must not retire anything")

            # The fifth evening -- not the fifth invocation -- retires it, on its
            # first repetition; the rest of that evening finds nothing to do.
            ledger.day = date(2026, 9, 19)
            retiring = await post_close_sync(dependencies, today=date(2026, 9, 19))
            after = await post_close_sync(dependencies, today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(retiring["status"], "blocked")
        self.assertEqual(retiring["results"][0]["ledger"]["consecutive_blocked_runs"],
                         MAX_CONSECUTIVE_BLOCKED_RUNS)
        self.assertTrue(retiring["results"][0]["ledger"]["retired"])
        self.assertEqual(len(ledger.receipts), 1)
        self.assertEqual(after["retired_dates"], ["2026-09-11"])
        self.assertEqual(after["lane_status"], "unchanged")

    async def test_a_provider_block_is_a_failure_not_a_skip(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "blocked", "trade_date": str(trade_date),
                    "blocked_by": PROVIDER_BLOCK_REASON, "reason": "adj_factor route refused"}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["outcome"], "failed")

    async def test_a_retired_date_drops_off_the_work_list(self):
        database = _Database(
            [{"trading_date": date(2026, 9, 17)}],
            ledger_rows=[{"as_of_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        def explode(*_args, **_kwargs):
            raise AssertionError("a retired date must not be fetched again")

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(explode)
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["pending_dates"], ["2026-09-17"])
        self.assertEqual(result["retired_dates"], ["2026-09-17"])
        self.assertEqual(result["results"], [])

    async def test_a_successful_date_clears_its_blocked_ledger_row(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "completed", "trade_date": str(trade_date)}

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

        cleared = [call for call in database.connection.calls
                   if "'consecutive_blocked_runs', 0" in call[0]]
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0][1], (blocked_date_run_key(date(2026, 9, 17)),))

    async def test_nothing_pending_is_reported_as_unchanged(self):
        result = await sync(self._dependencies(_Database([])), today=date(2026, 9, 19))
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["pending_dates"], [])

    async def test_post_close_entry_point_is_a_short_window_and_says_it_never_gates(self):
        database = _Database([])
        seen: dict[str, object] = {}
        import app.adjustment_factor_maintenance as module

        original = module.pending_dates

        def capture(_database, *, lookback_days, today=None, minimum_ratio=PENDING_COVERAGE_RATIO,
                    include_retired=False):
            seen["lookback_days"] = lookback_days
            seen["include_retired"] = include_retired
            return []

        module.pending_dates = capture
        try:
            result = await post_close_sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.pending_dates = original

        # No usable calendar answer: the window falls back to the documented
        # floor rather than to something shorter than the retirement rule.
        self.assertEqual(seen["lookback_days"], POST_CLOSE_LOOKBACK_DAYS)
        self.assertEqual(result["lookback_days"], POST_CLOSE_LOOKBACK_DAYS)
        # The job reports the retired dates itself, so it is the one caller
        # that asks for the raw coverage list.
        self.assertTrue(seen["include_retired"])
        self.assertTrue(result["non_gating"])
        # Nothing was pending: this is the one post-close verdict that may
        # seal a durable receipt.
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["lane_status"], "unchanged")
        self.assertFalse(result["retryable"])


class PostCloseLookbackWindowTests(unittest.IsolatedAsyncioTestCase):
    """The post-close window has to outlive the retirement counter.

    The counter's unit is a ledger DAY, and a date is only refused -- so only
    counted -- while it is still inside the lookback window.  Five CALENDAR
    days meant that under the only schedule installed (post-close, ``-Daily -At
    16:40`` with repetitions until ~22:40, returning early on Sat/Sun) a date
    blocked on a Tuesday was seen on four lane evenings and then left the
    window, so ``MAX_CONSECUTIVE_BLOCKED_RUNS = 5`` was unreachable and the
    date was retried every night forever.
    """

    @staticmethod
    def _lane_evenings(start: date, count: int) -> list[date]:
        """The evenings the installed post-close task actually reaches a date on."""
        evenings: list[date] = []
        cursor = start
        while len(evenings) < count:
            if cursor.weekday() < 5:  # run-post-close-pipeline.ps1: weekend -> skipped
                evenings.append(cursor)
            cursor += timedelta(days=1)
        return evenings

    def test_the_window_still_holds_a_date_on_the_evening_that_retires_it(self):
        for offset in range(7):
            trade_date = date(2026, 9, 14) + timedelta(days=offset)  # Mon .. Sun
            if trade_date.weekday() >= 5:
                continue  # no session, so no date to repair
            retiring_evening = self._lane_evenings(
                trade_date, MAX_CONSECUTIVE_BLOCKED_RUNS)[-1]
            span = (retiring_evening - trade_date).days
            self.assertLessEqual(
                span, POST_CLOSE_LOOKBACK_DAYS,
                f"{trade_date} leaves the post-close window {span - POST_CLOSE_LOOKBACK_DAYS} "
                "day(s) before it can accumulate MAX_CONSECUTIVE_BLOCKED_RUNS ledger days")

    def test_the_old_five_day_window_is_what_made_retirement_unreachable(self):
        """The negative control for the constant above."""
        tuesday = date(2026, 9, 15)
        retiring_evening = self._lane_evenings(tuesday, MAX_CONSECUTIVE_BLOCKED_RUNS)[-1]
        self.assertEqual(retiring_evening, date(2026, 9, 21))  # the following Monday
        self.assertGreater((retiring_evening - tuesday).days, 5)

    def test_the_pure_rule_never_narrows_the_window(self):
        today = date(2026, 9, 19)
        # No calendar coverage at all.
        self.assertEqual(post_close_lookback_days(today), POST_CLOSE_LOOKBACK_DAYS)
        # A calendar that cannot produce the requested number of sessions is
        # not evidence of a short window; it is no evidence at all.
        self.assertEqual(
            post_close_lookback_days(today, date(2026, 9, 17), sessions_found=2),
            POST_CLOSE_LOOKBACK_DAYS)
        # An ordinary week: the span is shorter than the floor, so the floor wins.
        self.assertEqual(
            post_close_lookback_days(today, date(2026, 9, 11),
                                     sessions_found=POST_CLOSE_LOOKBACK_SESSIONS),
            POST_CLOSE_LOOKBACK_DAYS)
        # A holiday week (six sessions spanning 20 days): the window grows.
        self.assertEqual(
            post_close_lookback_days(today, date(2026, 8, 30),
                                     sessions_found=POST_CLOSE_LOOKBACK_SESSIONS),
            20)
        # A calendar whose rows are in the future is ignored rather than
        # producing a negative window.
        self.assertEqual(
            post_close_lookback_days(today, date(2026, 9, 25),
                                     sessions_found=POST_CLOSE_LOOKBACK_SESSIONS),
            POST_CLOSE_LOOKBACK_DAYS)

    def test_the_window_covers_one_session_more_than_the_retirement_rule(self):
        self.assertEqual(POST_CLOSE_LOOKBACK_SESSIONS, MAX_CONSECUTIVE_BLOCKED_RUNS + 1)
        self.assertGreaterEqual(POST_CLOSE_LOOKBACK_DAYS, 14)

    def test_the_calendar_query_counts_distinct_open_sessions(self):
        connection = _Connection([], sessions={"oldest_session": date(2026, 8, 30), "sessions": 6})
        self.assertEqual(
            post_close_lookback_days_from_calendar(connection, date(2026, 9, 19)), 20)
        sql, params = connection.calls[0]
        self.assertEqual(params, (date(2026, 9, 19), POST_CLOSE_LOOKBACK_SESSIONS))
        self.assertIn("SELECT DISTINCT calendar_date", sql)
        self.assertIn("WHERE is_open AND calendar_date<=%s", sql)
        self.assertEqual(sql.count("%s"), len(params))

    async def test_post_close_sync_resolves_its_window_from_the_calendar(self):
        database = _Database([], sessions={"oldest_session": date(2026, 8, 30), "sessions": 6})
        dependencies = AdjustmentFactorMaintenanceDependencies(
            database=database, run_database=_run_database,
            longhu_source=_no_longhu, run_public=_no_public_call,
            safe_error_detail=lambda value, _limit: value,
        )
        result = await post_close_sync(dependencies, today=date(2026, 9, 19))
        self.assertEqual(result["lookback_days"], 20)
        work_list = next(call for call in database.connection.calls
                         if "FROM quant.canonical_bars_daily bar" in call[0])
        self.assertEqual(work_list[1][0], date(2026, 8, 30))

    async def test_an_explicit_window_still_wins(self):
        database = _Database([], sessions={"oldest_session": date(2026, 8, 30), "sessions": 6})
        dependencies = AdjustmentFactorMaintenanceDependencies(
            database=database, run_database=_run_database,
            longhu_source=_no_longhu, run_public=_no_public_call,
            safe_error_detail=lambda value, _limit: value,
        )
        result = await post_close_sync(dependencies, lookback_days=3, today=date(2026, 9, 19))
        self.assertEqual(result["lookback_days"], 3)
        self.assertEqual([call for call in database.connection.calls
                          if "quant.market_trade_calendar" in call[0]
                          and "oldest_session" in call[0]], [])


class _AutomationRunsLedger:
    """A stand-in for quant.automation_runs that honours the real statements.

    Only three statements reach it, and each is implemented exactly as
    ``automation_run_repository`` writes it, including the ON CONFLICT rule
    that makes a ``completed`` receipt sticky and every other status
    re-openable.  Nothing about the stage wrapper is faked: the tests below run
    the real ``record_stage_with_receipt`` against this ledger.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}

    @staticmethod
    def _plain(value):
        # finish_run wraps the summary in psycopg's Json adapter.
        return getattr(value, "obj", value)

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if text.startswith("INSERT INTO quant.automation_runs"):
            run_key = params[1]
            row = self.rows.get(run_key)
            if row is None:
                row = {"run_id": f"run-{len(self.rows) + 1}", "status": "running",
                       "output_summary": {}, "run_key": run_key}
                self.rows[run_key] = row
            elif row["status"] != "completed":
                row["status"] = "running"
            return _Result([dict(row)])
        if text.startswith("UPDATE quant.automation_runs SET status=%s,output_summary=%s"):
            status, summary, run_id = params
            row = next(item for item in self.rows.values() if item["run_id"] == run_id)
            row["status"], row["output_summary"] = status, self._plain(summary)
            return _Result([])
        if text.startswith("UPDATE quant.automation_runs SET status='failed'"):
            row = next(item for item in self.rows.values() if item["run_id"] == params[2])
            row["status"], row["error_message"] = "failed", params[1]
            return _Result([])
        raise AssertionError(f"unexpected statement against the ledger: {text}")


class _LedgerDatabase:
    def __init__(self, ledger):
        self.connection = ledger

    def transaction(self):
        class Context:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_args): return False
        return Context(self.connection)


class PostCloseStageReceiptTests(unittest.IsolatedAsyncioTestCase):
    """The lane's verdict as the post-close pipeline's DURABLE receipt.

    ``install-post-close-pipeline-task.ps1`` gives the pipeline task a
    repetition (~16:40-22:40) and ``run-post-close-pipeline.ps1`` re-POSTs the
    same ``trade_date`` on every repetition, while
    ``post_close_refresh.record_stage_with_receipt`` skips any stage whose
    ``quant.automation_runs`` row already says ``completed`` -- and normalizes
    every status it does not recognise, ``skipped`` included, to exactly that.
    So a coverage-skipped factor run recorded as ``completed`` burns the whole
    evening's remaining retries for that date.
    """

    def _dependencies(self, database, **overrides):
        base = dict(
            database=database, run_database=_run_database,
            longhu_source=_no_longhu, run_public=_no_public_call,
            safe_error_detail=lambda value, _limit: value,
        )
        base.update(overrides)
        return AdjustmentFactorMaintenanceDependencies(**base)

    async def _lane(self, outcomes, pending=(date(2026, 9, 17),)):
        """One real ``post_close_sync`` payload, with the provider replaced."""
        database = _Database([{"trading_date": value} for value in pending])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return dict(outcomes[str(trade_date)], trade_date=str(trade_date))

        original = module.repair_factor_date
        module.repair_factor_date = _per_date(fake_sync)
        try:
            return await post_close_sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.repair_factor_date = original

    @staticmethod
    async def _record(ledger, payload, *, calls):
        """Drive the real stage wrapper, i.e. the receipt normalization path."""
        async def run_database_blocking(action, *args, **_kwargs):
            return action(*args)

        async def stage_action():
            calls.append(payload)
            return payload

        return await record_stage_with_receipt(
            "adjustment_factors", date(2026, 9, 18), stage_action,
            db=_LedgerDatabase(ledger), run_database_blocking=run_database_blocking,
            safe_error_detail=lambda value, _limit: value,
        )

    @staticmethod
    def _receipt(ledger) -> dict:
        self_row = next(iter(ledger.rows.values()))
        return self_row

    async def test_the_wrapper_really_does_normalize_an_unknown_status_to_completed(self):
        """The mechanism the mapping exists for, pinned rather than assumed."""
        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, {"status": "skipped", "reason": "coverage"}, calls=calls)
        self.assertEqual(self._receipt(ledger)["status"], "completed")
        # ... and a 'completed' row makes the next repetition skip the stage.
        second = await self._record(ledger, {"status": "skipped"}, calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertTrue(second["resumed_from_receipt"])

    async def test_a_coverage_skipped_run_is_recorded_blocked_and_runs_again(self):
        payload = await self._lane({"2026-09-17": {
            "status": "blocked", "blocked_by": COVERAGE_BLOCK_REASON,
            "reason": "full-market daily bars are not ready"}})
        self.assertEqual(payload["lane_status"], "skipped")
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["unrepaired_dates"], ["2026-09-17"])
        self.assertIn("2026-09-17", payload["reason"])

        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, payload, calls=calls)
        receipt = self._receipt(ledger)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["output_summary"]["status"], "blocked")
        self.assertIn("still unrepaired", receipt["output_summary"]["reason"])
        # The lane's own verdict survives beside the normalized status, so the
        # receipt says 'this was a coverage skip', not just 'blocked'.
        self.assertEqual(receipt["output_summary"]["lane_status"], "skipped")
        self.assertTrue(receipt["output_summary"]["retryable"])

        # The next pipeline repetition re-opens the row and runs the stage.
        result = await self._record(ledger, payload, calls=calls)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("resumed_from_receipt", result)
        self.assertEqual(self._receipt(ledger)["status"], "blocked")

    async def test_a_repaired_run_seals_the_receipt_for_the_rest_of_the_evening(self):
        payload = await self._lane({"2026-09-17": {"status": "completed"}})
        self.assertEqual((payload["status"], payload["lane_status"]), ("completed", "completed"))
        self.assertFalse(payload["retryable"])

        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, payload, calls=calls)
        self.assertEqual(self._receipt(ledger)["status"], "completed")
        resumed = await self._record(ledger, payload, calls=calls)
        self.assertEqual(len(calls), 1)
        self.assertTrue(resumed["resumed_from_receipt"])

    async def test_a_mixed_run_that_still_owes_one_date_is_not_sealed(self):
        """The lane calls a run with one repair and one skip 'completed'."""
        payload = await self._lane(
            {"2026-09-17": {"status": "completed"},
             "2026-09-18": {"status": "blocked", "blocked_by": COVERAGE_BLOCK_REASON,
                            "reason": "thin cross-section"}},
            pending=(date(2026, 9, 17), date(2026, 9, 18)))
        self.assertEqual(payload["lane_status"], "completed")
        self.assertEqual(payload["skipped_dates"], 1)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["unrepaired_dates"], ["2026-09-18"])

        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, payload, calls=calls)
        self.assertEqual(self._receipt(ledger)["status"], "blocked")
        await self._record(ledger, payload, calls=calls)
        self.assertEqual(len(calls), 2)

    async def test_a_provider_failure_is_recorded_failed_and_runs_again(self):
        payload = await self._lane({"2026-09-17": {
            "status": "blocked", "blocked_by": PROVIDER_BLOCK_REASON,
            "reason": "adj_factor route refused"}})
        self.assertEqual((payload["lane_status"], payload["status"]), ("failed", "failed"))

        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, payload, calls=calls)
        self.assertEqual(self._receipt(ledger)["status"], "failed")
        await self._record(ledger, payload, calls=calls)
        self.assertEqual(len(calls), 2)

    async def test_nothing_pending_is_the_only_other_sealed_verdict(self):
        payload = await self._lane({}, pending=())
        self.assertEqual((payload["lane_status"], payload["status"]), ("unchanged", "unchanged"))
        ledger, calls = _AutomationRunsLedger(), []
        await self._record(ledger, payload, calls=calls)
        # 'unchanged' is not one of the four statuses the wrapper recognises,
        # so it is normalized to 'completed' -- which is correct here and only
        # here: no date was left behind.
        self.assertEqual(self._receipt(ledger)["status"], "completed")

    def test_the_mapping_table_is_pinned(self):
        self.assertEqual(POST_CLOSE_TERMINAL_LANE_STATUSES, ("completed", "unchanged"))
        cases = {
            "completed": "completed", "unchanged": "unchanged", "skipped": "blocked",
            "failed": "failed", "planned": "blocked", "": "blocked",
        }
        for lane_status, expected in cases.items():
            with self.subTest(lane_status=lane_status):
                self.assertEqual(post_close_stage_receipt({"status": lane_status})["status"], expected)
        # Counters win over a lane verdict that looks harmless.
        self.assertEqual(
            post_close_stage_receipt({"status": "completed", "skipped_dates": 1})["status"], "blocked")
        self.assertEqual(
            post_close_stage_receipt({"status": "completed", "failed_dates": 1})["status"], "failed")


class BlockedDateLedgerTests(unittest.TestCase):
    """The consecutive-block counter and its single retirement receipt."""

    def test_retired_dates_asks_for_the_documented_threshold(self):
        connection = _Connection([], ledger_rows=[{"as_of_date": date(2026, 9, 4)}])
        self.assertEqual(retired_dates(connection, [date(2026, 9, 4), date(2026, 9, 8)]),
                         {date(2026, 9, 4)})
        _sql, params = connection.calls[0]
        self.assertEqual(params[0], BLOCKED_DATE_TASK_KEY)
        self.assertEqual(params[1], [blocked_date_run_key(date(2026, 9, 4)),
                                     blocked_date_run_key(date(2026, 9, 8))])
        self.assertEqual(params[2], MAX_CONSECUTIVE_BLOCKED_RUNS)

    def test_an_empty_work_list_asks_the_ledger_nothing(self):
        connection = _Connection([])
        self.assertEqual(retired_dates(connection, []), set())
        self.assertEqual(connection.calls, [])

    def test_the_retirement_receipt_is_written_exactly_once(self):
        connection = _Connection([], blocked_summary={
            "consecutive_blocked_runs": MAX_CONSECUTIVE_BLOCKED_RUNS})
        state = record_blocked_date(connection, date(2026, 9, 4), "thin cross-section")
        self.assertTrue(state["retired"])
        self.assertTrue(state["retirement_receipt_written"])
        receipts = [call for call in connection.calls if "data_quality_issues" in call[0]]
        self.assertEqual(len(receipts), 1)
        self.assertIn("adjustment_factor_date_retired", receipts[0][0])

        # Already-receipted: loud once, silent afterwards.
        connection.blocked_summary = {"consecutive_blocked_runs": MAX_CONSECUTIVE_BLOCKED_RUNS + 3,
                                      "retirement_receipt_written": True}
        again = record_blocked_date(connection, date(2026, 9, 4), "thin cross-section")
        self.assertTrue(again["retired"])
        self.assertFalse(again["retirement_receipt_written"])
        self.assertEqual(
            len([call for call in connection.calls if "data_quality_issues" in call[0]]), 1)

    def test_clearing_a_date_also_resolves_its_retirement_receipt(self):
        """A retired date that is fetched again is back on the work list.

        Leaving the ``adjustment_factor_date_retired`` row open would keep a
        warning in quant.data_quality_issues that nothing can ever resolve.
        """
        connection = _Connection([])
        clear_blocked_date(connection, date(2026, 9, 4))
        resolved = [call for call in connection.calls
                    if "data_quality_issues SET resolved_at" in call[0]]
        self.assertEqual(len(resolved), 1)
        self.assertIn("code='adjustment_factor_date_retired'", resolved[0][0])
        self.assertIn("resolved_at IS NULL", resolved[0][0])
        self.assertEqual(resolved[0][1], (date(2026, 9, 4),))

    def test_below_the_threshold_nothing_is_retired_and_no_receipt_is_written(self):
        connection = _Connection([], blocked_summary={
            "consecutive_blocked_runs": MAX_CONSECUTIVE_BLOCKED_RUNS - 1})
        state = record_blocked_date(connection, date(2026, 9, 4), "thin cross-section")
        self.assertFalse(state["retired"])
        self.assertEqual([call for call in connection.calls if "data_quality_issues" in call[0]], [])


class BlockedLedgerDayTests(unittest.TestCase):
    """The second half of the ledger key: which day a refusal belongs to."""

    def test_one_evening_and_the_dawn_run_that_serves_it_are_one_ledger_day(self):
        """The 12:00 CST boundary is what makes the counter mean "evenings".

        The post-close pipeline repeats every ``RetryIntervalMinutes`` from
        ~15:30 to ~22:40, and the 04:30 maintenance task works the SAME backlog
        before the next session.  All of them must land on one ledger day, or
        five "days" would be reached in two and a half evenings.
        """
        evening = datetime(2026, 9, 15, 15, 31, tzinfo=CHINA)
        self.assertEqual(blocked_ledger_day(evening), date(2026, 9, 15))
        self.assertEqual(blocked_ledger_day(datetime(2026, 9, 15, 22, 40, tzinfo=CHINA)),
                         date(2026, 9, 15))
        self.assertEqual(blocked_ledger_day(datetime(2026, 9, 16, 4, 30, tzinfo=CHINA)),
                         date(2026, 9, 15))
        # The next evening is a new ledger day.
        self.assertEqual(blocked_ledger_day(datetime(2026, 9, 16, 15, 31, tzinfo=CHINA)),
                         date(2026, 9, 16))
        # A caller in another timezone gets the exchange-local answer.
        self.assertEqual(
            blocked_ledger_day(datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)),  # 22:00 CST
            date(2026, 9, 15))

    def test_the_statement_shape_keys_the_counter_on_the_day_not_the_invocation(self):
        """SHAPE ONLY -- the statement's BEHAVIOUR is proved by PostgreSQL.

        Under plain pytest nothing executes ``_RECORD_BLOCKED_DATE_SQL``: the
        counting decision in every test above comes from
        :class:`_BlockedDateLedger`, a hand-written model of it, and the real
        ``ON CONFLICT`` branch runs only in
        :class:`BlockedDateLedgerPostgresTests` (``PGHOST``).  This test is
        therefore the whole default gate on the statement itself, so it pins
        the two CASE arms by their exact bodies, assembled from the module's
        own fragments.  Keying only on the word ``CASE WHEN`` let an INVERTED
        statement -- increment when the day IS already counted -- pass, which
        is exactly the blocker it exists to catch.
        """
        statement = " ".join(module._RECORD_BLOCKED_DATE_SQL.split())
        counted = " ".join(module._COUNTED_TODAY_SQL.split())
        runs = " ".join(module._BLOCKED_RUNS_SQL.split())
        days = " ".join(module._BLOCKED_DAYS_SQL.split())
        ledger_day = " ".join(module._LEDGER_DAY_JSON_SQL.split())
        self.assertIn("@>", counted)  # "have we already counted this day?"
        self.assertIn(f"interval '{module.BLOCKED_LEDGER_DAY_BOUNDARY_HOUR} hours'", statement)
        # The counter: +1 belongs to the ELSE arm, and ONLY to it.
        self.assertIn(
            f"'consecutive_blocked_runs', CASE WHEN {counted} THEN {runs} ELSE {runs}+1 END",
            statement)
        # The day list: the new day is appended in the same ELSE arm, so the
        # counter and the days it is made of can never disagree by branch.
        self.assertIn(
            f"'blocked_days', CASE WHEN {counted} THEN ({days}) "
            f"ELSE ({days}) || {ledger_day} END",
            statement)
        # ...and the unconditional per-invocation form is gone.
        self.assertNotIn("'consecutive_blocked_runs', coalesce", statement)
        # A fresh row starts its own day list rather than a bare counter.
        self.assertIn("'blocked_days', jsonb_build_array(", statement)

    def test_an_inverted_case_fails_the_shape_test(self):
        """The negative control for the test above, executed.

        Swapping the two arms restores the original blocker (every invocation
        counts once the day is recorded).  The assertions must not survive it.
        """
        counted = " ".join(module._COUNTED_TODAY_SQL.split())
        runs = " ".join(module._BLOCKED_RUNS_SQL.split())
        inverted = " ".join(module._RECORD_BLOCKED_DATE_SQL.split()).replace(
            f"'consecutive_blocked_runs', CASE WHEN {counted} THEN {runs} ELSE {runs}+1 END",
            f"'consecutive_blocked_runs', CASE WHEN {counted} THEN {runs}+1 ELSE {runs} END")
        self.assertNotEqual(inverted, " ".join(module._RECORD_BLOCKED_DATE_SQL.split()),
                            "the replacement must actually match the shipped statement")
        # The old assertion could not tell the two apart; the new one can.
        self.assertIn("'consecutive_blocked_runs', CASE WHEN", inverted)
        self.assertNotIn(
            f"'consecutive_blocked_runs', CASE WHEN {counted} THEN {runs} ELSE {runs}+1 END",
            inverted)

    def test_repeating_the_same_evening_a_dozen_times_counts_once(self):
        """The exact shape that retired a date inside one evening."""
        ledger = _BlockedDateLedger(day=date(2026, 9, 15))
        connection = _Connection([], ledger=ledger)
        for _repetition in range(12):
            state = record_blocked_date(connection, date(2026, 9, 11), "thin cross-section")
            self.assertEqual(state["consecutive_blocked_runs"], 1)
            self.assertEqual(state["blocked_days"], ["2026-09-15"])
            self.assertFalse(state["retired"])
        self.assertEqual(ledger.receipts, [])
        self.assertEqual(retired_dates(connection, [date(2026, 9, 11)]), set())

    def test_five_separate_evenings_retire_the_date_exactly_once(self):
        ledger = _BlockedDateLedger(day=date(2026, 9, 15))
        connection = _Connection([], ledger=ledger)
        seen = []
        for offset in range(MAX_CONSECUTIVE_BLOCKED_RUNS):
            ledger.day = date(2026, 9, 15) + timedelta(days=offset)
            for _repetition in range(12):  # one evening of pipeline repetitions
                state = record_blocked_date(connection, date(2026, 9, 11), "thin cross-section")
            seen.append(state["consecutive_blocked_runs"])
            self.assertEqual(state["retired"], offset + 1 == MAX_CONSECUTIVE_BLOCKED_RUNS)
        self.assertEqual(seen, [1, 2, 3, 4, 5])
        self.assertEqual(state["blocked_days"],
                         ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19"])
        self.assertEqual(len(ledger.receipts), 1, "the receipt is written on the retiring day only")
        self.assertIn("2026-09-15, 2026-09-16", ledger.receipts[0][0])
        self.assertEqual(retired_dates(connection, [date(2026, 9, 11)]), {date(2026, 9, 11)})

        # A sixth evening stays silent, and a successful fetch re-opens the date.
        ledger.day = date(2026, 9, 20)
        record_blocked_date(connection, date(2026, 9, 11), "thin cross-section")
        self.assertEqual(len(ledger.receipts), 1)
        clear_blocked_date(connection, date(2026, 9, 11))
        self.assertEqual(retired_dates(connection, [date(2026, 9, 11)]), set())

    def test_the_receipt_says_which_of_the_two_numbers_it_is_showing(self):
        """A row carried over from the invocation era retires with one day.

        ``consecutive_blocked_runs`` is deliberately ahead of ``blocked_days``
        on such a row (the count is kept, the days start now), so printing the
        list inside "on 5 separate days (...)" contradicts itself.  The receipt
        is the durable, human-facing record of the retirement; it has to say
        what it can prove.
        """
        phrase = module._blocked_days_receipt_phrase
        self.assertEqual(phrase(2, ["2026-09-15", "2026-09-16"]), "(2026-09-15, 2026-09-16)")
        self.assertIn("recorded days: 2026-09-19", phrase(5, ["2026-09-19"]))
        self.assertIn("predate the day key", phrase(5, ["2026-09-19"]))
        self.assertIn("none", phrase(5, []))

        ledger = _BlockedDateLedger(day=date(2026, 9, 19))
        run_key = blocked_date_run_key(date(2026, 9, 11))
        # The shape an upgrade leaves behind: a count, no day list.
        ledger.rows[run_key] = {
            "as_of_date": date(2026, 9, 11), "run_key": run_key,
            "output_summary": {"consecutive_blocked_runs": MAX_CONSECUTIVE_BLOCKED_RUNS - 1,
                               "blocked_days": []}}
        connection = _Connection([], ledger=ledger)
        state = record_blocked_date(connection, date(2026, 9, 11), "thin cross-section")
        self.assertEqual(state["consecutive_blocked_runs"], MAX_CONSECUTIVE_BLOCKED_RUNS)
        self.assertEqual(state["blocked_days"], ["2026-09-19"])
        self.assertTrue(state["retired"])
        message = ledger.receipts[0][0]
        self.assertIn(f"on {MAX_CONSECUTIVE_BLOCKED_RUNS} separate days", message)
        self.assertIn("recorded days: 2026-09-19", message)
        self.assertNotIn(
            f"on {MAX_CONSECUTIVE_BLOCKED_RUNS} separate days (2026-09-19)", message,
            "five days and one listed day must not be printed as if they agreed")


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class BlockedDateLedgerPostgresTests(unittest.TestCase):
    """The ledger SQL itself, on real PostgreSQL, on a fixture-only key.

    ``jsonb_build_object`` concatenation, ``= ANY(%s)`` over a text array and
    the automation_runs status CHECK are all things a fake connection cannot
    prove; the counter is the whole point of the retirement rule.
    """

    trade_date = date(2099, 4, 2)

    #: How many times the post-close stage reaches one date in one evening
    #: (every ``RetryIntervalMinutes`` from ~15:30 to ~22:40).
    repetitions_per_evening = 12

    def _cleanup(self, connection) -> None:
        connection.execute(
            "DELETE FROM quant.automation_runs WHERE run_key=%s",
            (blocked_date_run_key(self.trade_date),))
        connection.execute(
            "DELETE FROM quant.data_quality_issues WHERE code='adjustment_factor_date_retired' "
            "AND trading_date=%s", (self.trade_date,))

    def _end_the_evening(self, connection) -> None:
        """Age the ledger by one day, which is what tomorrow does to it.

        The statement asks PostgreSQL for "today"; nothing in the test may
        change the server's clock, so the evenings already recorded are moved
        one day into the past instead.  The next invocation then sees exactly
        the row yesterday's evening would have left behind.
        """
        connection.execute(
            """UPDATE quant.automation_runs
                  SET output_summary = output_summary || jsonb_build_object('blocked_days',
                      (SELECT coalesce(jsonb_agg((day::date - 1)::text ORDER BY day::date),'[]'::jsonb)
                         FROM jsonb_array_elements_text(output_summary->'blocked_days') AS day))
                WHERE run_key=%s""",
            (blocked_date_run_key(self.trade_date),))

    def test_the_ledger_day_is_the_same_in_sql_and_in_python(self):
        """The counter is decided in SQL; every caller names the day in Python."""
        from app.adjustment_factor_maintenance import _LEDGER_DAY_SQL
        from app.main import db

        with db.transaction() as connection:
            row = connection.execute(f"SELECT {_LEDGER_DAY_SQL} AS ledger_day").fetchone()
        self.assertEqual(row["ledger_day"], blocked_ledger_day())

    def test_a_whole_evening_of_repetitions_counts_as_one_day(self):
        """The blocker: MAX_CONSECUTIVE_BLOCKED_RUNS must mean evenings."""
        from app.main import db

        with db.transaction() as connection:
            self._cleanup(connection)
            try:
                for repetition in range(self.repetitions_per_evening):
                    state = record_blocked_date(connection, self.trade_date, "thin cross-section")
                    self.assertEqual(state["consecutive_blocked_runs"], 1, f"repetition {repetition}")
                    self.assertEqual(state["blocked_days"], [str(blocked_ledger_day())])
                    self.assertFalse(state["retired"])
                self.assertEqual(retired_dates(connection, [self.trade_date]), set())
                receipts = connection.execute(
                    "SELECT count(*)::int AS rows FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s",
                    (self.trade_date,)).fetchone()["rows"]
                self.assertEqual(receipts, 0)
            finally:
                self._cleanup(connection)

    def test_the_counter_retires_once_and_a_success_clears_it(self):
        from app.adjustment_factor_maintenance import clear_blocked_date
        from app.main import db

        with db.transaction() as connection:
            self._cleanup(connection)
            try:
                for expected in range(1, MAX_CONSECUTIVE_BLOCKED_RUNS + 1):
                    if expected > 1:
                        self._end_the_evening(connection)
                    written = 0
                    for repetition in range(self.repetitions_per_evening):
                        state = record_blocked_date(
                            connection, self.trade_date, "thin cross-section")
                        written += int(state["retirement_receipt_written"])
                        self.assertEqual(state["consecutive_blocked_runs"], expected,
                                         f"evening {expected}, repetition {repetition}")
                        self.assertEqual(len(state["blocked_days"]), expected)
                        self.assertEqual(state["last_blocked_day"], str(blocked_ledger_day()))
                    self.assertEqual(state["retired"], expected >= MAX_CONSECUTIVE_BLOCKED_RUNS)
                    self.assertEqual(
                        written, int(expected == MAX_CONSECUTIVE_BLOCKED_RUNS),
                        "the retirement receipt must be written on exactly the retiring evening, "
                        "once, however many times the stage runs that evening")

                self.assertEqual(retired_dates(connection, [self.trade_date]), {self.trade_date})
                receipts = connection.execute(
                    "SELECT count(*)::int AS rows FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s",
                    (self.trade_date,)).fetchone()["rows"]
                self.assertEqual(receipts, 1)

                # One more run the same evening changes nothing at all.
                same = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(same["consecutive_blocked_runs"], MAX_CONSECUTIVE_BLOCKED_RUNS)
                self.assertFalse(same["retirement_receipt_written"])

                # A sixth evening counts, and stays silent rather than alerting again.
                self._end_the_evening(connection)
                again = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(again["consecutive_blocked_runs"], MAX_CONSECUTIVE_BLOCKED_RUNS + 1)
                self.assertFalse(again["retirement_receipt_written"])
                receipts = connection.execute(
                    "SELECT count(*)::int AS rows FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s",
                    (self.trade_date,)).fetchone()["rows"]
                self.assertEqual(receipts, 1)

                # The retired date carries its ledger evidence, and the work
                # list stops reporting it as pending.
                details = retired_date_details(connection, [self.trade_date])
                self.assertEqual(details[self.trade_date]["run_key"],
                                 blocked_date_run_key(self.trade_date))
                self.assertEqual(details[self.trade_date]["reason"], "thin cross-section")
                self.assertGreaterEqual(
                    details[self.trade_date]["consecutive_blocked_runs"],
                    MAX_CONSECUTIVE_BLOCKED_RUNS)

                # A successful fetch puts the date back on the work list and
                # resolves the one-time retirement warning.
                clear_blocked_date(connection, self.trade_date)
                self.assertEqual(retired_dates(connection, [self.trade_date]), set())
                self.assertEqual(retired_date_details(connection, [self.trade_date]), {})
                open_receipts = connection.execute(
                    "SELECT count(*)::int AS rows FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s "
                    "AND resolved_at IS NULL", (self.trade_date,)).fetchone()["rows"]
                self.assertEqual(open_receipts, 0)
                row = connection.execute(
                    "SELECT status,output_summary FROM quant.automation_runs WHERE run_key=%s",
                    (blocked_date_run_key(self.trade_date),)).fetchone()
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["output_summary"]["consecutive_blocked_runs"], 0)
                self.assertEqual(row["output_summary"]["blocked_days"], [])
                self.assertFalse(row["output_summary"]["retirement_receipt_written"])

                # ...and the cleared row starts counting from today again,
                # rather than resuming the retired count on its next refusal.
                restarted = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(restarted["consecutive_blocked_runs"], 1)
                self.assertEqual(restarted["blocked_days"], [str(blocked_ledger_day())])
            finally:
                self._cleanup(connection)

    def test_a_row_written_before_the_day_key_keeps_its_count(self):
        """The ledger predates ``blocked_days``; an upgrade must not reset it.

        A row from the invocation-counting era has no ``blocked_days`` at all.
        It must neither lose its progress (a reset would give a permanently
        thin date five more evenings) nor double-count (two entries for one
        evening would retire it sooner than the rule says).
        """
        from app.main import db

        with db.transaction() as connection:
            self._cleanup(connection)
            try:
                connection.execute(
                    """INSERT INTO quant.automation_runs(
                           task_key,run_key,cadence,as_of_date,status,methodology_version,
                           input_summary,output_summary,finished_at)
                       VALUES(%s,%s,'daily',%s,'blocked',%s,'{}'::jsonb,
                              '{"consecutive_blocked_runs": 3, "reason": "legacy"}'::jsonb,now())""",
                    (BLOCKED_DATE_TASK_KEY, blocked_date_run_key(self.trade_date),
                     self.trade_date, BLOCKED_DATE_TASK_KEY))
                first = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(first["consecutive_blocked_runs"], 4)
                self.assertEqual(first["blocked_days"], [str(blocked_ledger_day())])
                repeat = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(repeat["consecutive_blocked_runs"], 4)
                self.assertEqual(repeat["blocked_days"], [str(blocked_ledger_day())])
                self.assertFalse(repeat["retired"])
            finally:
                self._cleanup(connection)

    def test_a_legacy_row_that_retires_says_only_what_it_can_prove(self):
        """The receipt must not read as "5 days (one date)" on an upgraded row."""
        from app.main import db

        with db.transaction() as connection:
            self._cleanup(connection)
            try:
                connection.execute(
                    """INSERT INTO quant.automation_runs(
                           task_key,run_key,cadence,as_of_date,status,methodology_version,
                           input_summary,output_summary,finished_at)
                       VALUES(%s,%s,'daily',%s,'blocked',%s,'{}'::jsonb,%s::jsonb,now())""",
                    (BLOCKED_DATE_TASK_KEY, blocked_date_run_key(self.trade_date),
                     self.trade_date, BLOCKED_DATE_TASK_KEY,
                     f'{{"consecutive_blocked_runs": {MAX_CONSECUTIVE_BLOCKED_RUNS - 1}, '
                     '"reason": "legacy"}'))
                state = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(state["consecutive_blocked_runs"], MAX_CONSECUTIVE_BLOCKED_RUNS)
                self.assertEqual(state["blocked_days"], [str(blocked_ledger_day())])
                self.assertTrue(state["retired"])
                message = connection.execute(
                    "SELECT message FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s",
                    (self.trade_date,)).fetchone()["message"]
                self.assertIn(f"on {MAX_CONSECUTIVE_BLOCKED_RUNS} separate days", message)
                self.assertIn(f"recorded days: {blocked_ledger_day()}", message)
                self.assertNotIn(
                    f"on {MAX_CONSECUTIVE_BLOCKED_RUNS} separate days ({blocked_ledger_day()})",
                    message)
            finally:
                self._cleanup(connection)

    def test_the_post_close_window_query_executes_on_postgres(self):
        """The window statement is new SQL; nothing else executes it.

        The answer depends on this database's calendar coverage, so the
        assertion is on the CONTRACT, not on a number: whatever comes back, the
        window is never shorter than the documented floor and never negative.
        """
        from app.adjustment_factor_maintenance import (
            POST_CLOSE_LOOKBACK_SESSIONS,
            POST_CLOSE_LOOKBACK_SESSIONS_SQL,
            post_close_lookback_days_from_calendar,
        )
        from app.main import db

        with db.transaction() as connection:
            row = connection.execute(
                POST_CLOSE_LOOKBACK_SESSIONS_SQL,
                (china_today(), POST_CLOSE_LOOKBACK_SESSIONS)).fetchone()
            days = post_close_lookback_days_from_calendar(connection, china_today())
        self.assertEqual(sorted(row), ["oldest_session", "sessions"])
        self.assertGreaterEqual(days, POST_CLOSE_LOOKBACK_DAYS)
        # A calendar that can answer must cover at least the retirement rule.
        if row["sessions"] == POST_CLOSE_LOOKBACK_SESSIONS:
            self.assertGreaterEqual(days, (china_today() - row["oldest_session"]).days)

    def test_the_work_list_and_readiness_window_queries_execute_on_postgres(self):
        from app.main import db
        from app.stock_study_readiness_repository import ADJUSTMENT_WINDOW_SQL

        with db.transaction() as connection:
            self.assertEqual(
                pending_dates_between(connection, date(2099, 4, 1), date(2099, 4, 30)), [])
            row = connection.execute(
                ADJUSTMENT_WINDOW_SQL,
                ("999982.SZ", date(2099, 4, 1), date(2099, 4, 30),
                 "999982.SZ", date(2099, 4, 1), date(2099, 4, 30))).fetchone()
        self.assertEqual(int(row["rows"]), 0)
        self.assertEqual(int(row["settled_sessions"]), 0)
        self.assertEqual(list(row["uncovered_dates"]), [])


class StatusSummaryTests(unittest.TestCase):
    """The one line an operator reads instead of opening the JSON.

    Pure on purpose: the sentence is assembled from the report it is handed,
    so it can be pinned here and cannot quietly start disagreeing with the
    numbers printed above it.
    """

    @staticmethod
    def _report(**overrides):
        report = {
            "generated_for": {"start_date": "2026-09-05", "end_date": "2026-09-19",
                              "minimum_coverage_ratio": 0.95},
            "dates": [
                {"trading_date": "2026-09-17", "daily_rows": 5200, "adjustment_rows": 5200,
                 "pending": False, "retired": None},
                {"trading_date": "2026-09-18", "daily_rows": 5200, "adjustment_rows": 0,
                 "pending": True, "retired": None},
                {"trading_date": "2026-09-19", "daily_rows": 5100, "adjustment_rows": 0,
                 "pending": False, "retired": {"run_key": "adjustment-factor-blocked:2026-09-19",
                                               "reason": "coverage", "blocked_days": [],
                                               "consecutive_blocked_runs": 5, "retired_at": None}},
            ],
            "pending_dates": ["2026-09-18"],
            "retired_dates": ["2026-09-19"],
            "identity_factor_leaks": {"canonical_bars_daily": 0, "market_bars_daily": 0},
            "factor_fetch_runs": [{"provider_key": "tushare_super_get", "status": "completed",
                                   "row_count": 5567, "finished_at": "2026-09-19 04:31:02+08:00"}],
            "provider_capability": {"available": True,
                                    "preferred_providers": ["super_get", "super", "primary"]},
            "post_close_stage_receipt": {"status": "blocked", "as_of_date": "2026-09-18"},
        }
        report.update(overrides)
        return report

    def test_the_summary_names_the_window_the_counts_and_the_last_fetch(self):
        self.assertEqual(
            module.status_summary(self._report()),
            "2026-09-05..2026-09-19: 3 settled date(s), 1 complete, 1 pending, 1 retired; "
            "identity factor leaks 0; "
            "last fetch tushare_super_get completed rows=5567 at 2026-09-19 04:31:02+08:00; "
            "route available (super_get, super, primary); "
            "post-close receipt blocked for 2026-09-18")

    def test_a_retired_date_is_never_counted_as_complete(self):
        # It has no factors and nobody is going to fetch them; calling it
        # complete is the one reading that would let an operator stop looking.
        summary = module.status_summary(self._report())
        self.assertIn("1 complete, 1 pending, 1 retired", summary)

    def test_an_empty_estate_still_produces_one_readable_line(self):
        summary = module.status_summary(self._report(
            dates=[], pending_dates=[], retired_dates=[], factor_fetch_runs=[],
            provider_capability={"available": None, "note": "not consulted"},
            post_close_stage_receipt=None))
        self.assertEqual(
            summary,
            "2026-09-05..2026-09-19: 0 settled date(s), 0 complete, 0 pending, 0 retired; "
            "identity factor leaks 0; no adj_factor fetch run on record; "
            "no post-close factor-stage receipt")
        self.assertNotIn("\n", summary)

    def test_a_leak_on_either_guarded_table_is_reported(self):
        summary = module.status_summary(self._report(
            identity_factor_leaks={"canonical_bars_daily": 35573, "market_bars_daily": 0}))
        self.assertIn("identity factor leaks 35573", summary)

    def test_the_summary_is_ascii_only_because_the_task_console_is_gbk(self):
        # A blocked reason can carry Chinese text; it must never reach the line.
        summary = module.status_summary(self._report(
            post_close_stage_receipt={"status": "blocked", "as_of_date": "2026-09-18",
                                      "output_summary": {"reason": "覆盖率不足"}}))
        summary.encode("ascii")

    def test_a_declared_but_unverified_route_is_not_reported_as_available(self):
        summary = module.status_summary(self._report(
            provider_capability={"available": False, "preferred_providers": ["primary"]}))
        self.assertIn("route unverified (primary)", summary)


class MaintenanceCliTests(unittest.TestCase):
    """The CLI wrapper is the 04:00-08:00 entry point and the repair tool."""

    @staticmethod
    def _module():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "scripts" / "adjustment-factor-maintenance.py"
        spec = importlib.util.spec_from_file_location("adjustment_factor_maintenance_cli", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_cli_parses_the_documented_sync_contract(self):
        module = self._module()
        args = module.parse_args(["sync", "--lookback-days", "45", "--env-file", "x.env", "--dry-run"])
        self.assertEqual((args.command, args.lookback_days, args.env_file, args.dry_run),
                         ("sync", 45, "x.env", True))
        defaults = module.parse_args(["sync"])
        self.assertEqual((defaults.lookback_days, defaults.dry_run), (30, False))

    def test_cli_parses_the_read_only_status_contract(self):
        module = self._module()
        args = module.parse_args(["status", "--lookback-days", "7", "--env-file", "x.env"])
        self.assertEqual((args.command, args.lookback_days, args.env_file), ("status", 7, "x.env"))
        self.assertEqual(module.parse_args(["status"]).lookback_days, 30)
        # A report must not be able to fetch or repair anything, so it carries
        # no switch that could turn it into one.
        self.assertFalse(hasattr(module.parse_args(["status"]), "dry_run"))

    def test_cli_parses_the_repair_and_validate_contracts(self):
        module = self._module()
        dry = module.parse_args(["repair"])
        self.assertEqual((dry.command, dry.apply, dry.from_date, dry.to_date), ("repair", False, None, None))
        applied = module.parse_args(["repair", "--from", "2026-08-27", "--to", "2026-09-18", "--apply"])
        self.assertEqual((applied.from_date, applied.to_date, applied.apply),
                         (date(2026, 8, 27), date(2026, 9, 18), True))
        validate = module.parse_args(["validate"])
        self.assertEqual((validate.from_date, validate.to_date), (date(2026, 6, 1), date(2026, 8, 26)))

    def test_the_dry_run_and_validation_use_a_server_enforced_read_only_connection(self):
        from pathlib import Path

        source = Path(self._module().__file__).read_text(encoding="utf-8")
        self.assertIn("default_transaction_read_only=on", source)
        # The write path (app.main's pool) is imported only after both read-only
        # branches have returned.
        read_only_branch = source.index('if args.command == "repair" and not args.apply:')
        self.assertLess(read_only_branch, source.index("from app.main import"))
        self.assertLess(source.index('if args.command == "validate":'), source.index("from app.main import"))

    def test_env_file_loader_never_echoes_a_value(self):
        import os
        import tempfile
        from pathlib import Path

        module = self._module()
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "runtime.env"
            env_file.write_text(
                "# comment\nPGHOST=127.0.0.1\n\nPGPORT=55432\n", encoding="utf-8")
            previous = dict(os.environ)
            try:
                self.assertEqual(module.load_env_file(str(env_file)), 2)
                self.assertEqual(os.environ["PGHOST"], "127.0.0.1")
            finally:
                os.environ.clear()
                os.environ.update(previous)
        source = Path(module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("print(os.environ", source)
        self.assertIn("ensure_ascii=True", source)


if __name__ == "__main__":
    unittest.main()
