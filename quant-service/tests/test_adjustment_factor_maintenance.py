"""Coverage for the out-of-band cumulative adjustment-factor repair lane.

The job exists because the settled cross-section and the corporate-action
factor now come from different providers: a trading date can land with
complete bars and limits while its factors are still missing.  It runs in the
04:00-08:00 maintenance window, never inside the post-close pipeline.
"""

from __future__ import annotations

from datetime import date
import os
import unittest

from app.adjustment_factor_maintenance import (
    AdjustmentFactorMaintenanceDependencies,
    BLOCKED_DATE_TASK_KEY,
    MAX_CONSECUTIVE_BLOCKED_RUNS,
    PENDING_COVERAGE_RATIO,
    POST_CLOSE_LOOKBACK_DAYS,
    POST_CLOSE_TERMINAL_LANE_STATUSES,
    blocked_date_run_key,
    china_today,
    clear_blocked_date,
    pending_and_retired_dates_between,
    pending_dates,
    pending_dates_between,
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


class _Connection:
    """A connection that answers per statement rather than one canned result."""

    def __init__(self, rows, ledger_rows=None, blocked_summary=None):
        self.rows = rows
        self.ledger_rows = ledger_rows or []
        self.blocked_summary = blocked_summary or {"consecutive_blocked_runs": 1}
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.calls.append((text, params))
        if "quant.automation_runs" in text and text.startswith("SELECT as_of_date"):
            return _Result(self.ledger_rows)
        if "INSERT INTO quant.automation_runs" in text:
            return _Result([{"output_summary": dict(self.blocked_summary)}])
        if "quant.automation_runs" in text or "data_quality_issues" in text:
            return _Result([])
        return _Result(self.rows)


class _Database:
    def __init__(self, rows, ledger_rows=None, blocked_summary=None):
        self.connection = _Connection(rows, ledger_rows, blocked_summary)

    def transaction(self):
        class Context:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_args): return False
        return Context(self.connection)


async def _run_database(action, *args, **_kwargs):
    return action(*args)


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
                                             "retired_at": "2026-09-18 04:31:00"}}])
        pending, retired = pending_and_retired_dates_between(
            connection, date(2026, 9, 1), date(2026, 9, 18))
        self.assertEqual(pending, [date(2026, 9, 4)])
        self.assertEqual(retired[date(2026, 9, 15)], {
            "run_key": "adjustment-factor-blocked:2026-09-15", "reason": "thin cross-section",
            "consecutive_blocked_runs": 6, "retired_at": "2026-09-18 04:31:00"})

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
            call_tushare_api=None, parse_tushare_date=None, persist_tushare_rows=None,
            persist_blocked=None, safe_error_detail=lambda value, _limit: value,
            executor_saturated_error=RuntimeError, record_provider_success=None,
            record_provider_failure=None, record_provider_api_capability=None,
        )
        base.update(overrides)
        return AdjustmentFactorMaintenanceDependencies(**base)

    async def test_dry_run_issues_no_provider_call_and_no_write(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        def explode(*_args, **_kwargs):
            raise AssertionError("a dry run must not reach the controls sync")

        original = module.sync_daily_controls
        module.sync_daily_controls = explode
        try:
            result = await sync(self._dependencies(database), lookback_days=30, dry_run=True,
                                today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

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

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

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

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

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

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

        # The factor lane cannot repair a thin daily cross-section, so this is
        # a report, not a failure: a scheduled task must not alert forever.
        self.assertEqual(result["status"], "skipped")
        self.assertEqual((result["completed_dates"], result["skipped_dates"], result["failed_dates"]),
                         (0, 1, 0))
        self.assertEqual(result["results"][0]["outcome"], "skipped")
        self.assertEqual(result["results"][0]["reason"], "full-market daily bars are not ready")
        self.assertEqual(result["results"][0]["ledger"]["consecutive_blocked_runs"], 1)
        self.assertFalse(result["results"][0]["ledger"]["retired"])

    async def test_a_provider_block_is_a_failure_not_a_skip(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "blocked", "trade_date": str(trade_date),
                    "blocked_by": PROVIDER_BLOCK_REASON, "reason": "adj_factor route refused"}

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["results"][0]["outcome"], "failed")

    async def test_a_retired_date_drops_off_the_work_list(self):
        database = _Database(
            [{"trading_date": date(2026, 9, 17)}],
            ledger_rows=[{"as_of_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        def explode(*_args, **_kwargs):
            raise AssertionError("a retired date must not be fetched again")

        original = module.sync_daily_controls
        module.sync_daily_controls = explode
        try:
            result = await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["pending_dates"], ["2026-09-17"])
        self.assertEqual(result["retired_dates"], ["2026-09-17"])
        self.assertEqual(result["results"], [])

    async def test_a_successful_date_clears_its_blocked_ledger_row(self):
        database = _Database([{"trading_date": date(2026, 9, 17)}])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return {"status": "completed", "trade_date": str(trade_date)}

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            await sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

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

        self.assertEqual(seen["lookback_days"], POST_CLOSE_LOOKBACK_DAYS)
        # The job reports the retired dates itself, so it is the one caller
        # that asks for the raw coverage list.
        self.assertTrue(seen["include_retired"])
        self.assertTrue(result["non_gating"])
        # Nothing was pending: this is the one post-close verdict that may
        # seal a durable receipt.
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["lane_status"], "unchanged")
        self.assertFalse(result["retryable"])


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
            call_tushare_api=None, parse_tushare_date=None, persist_tushare_rows=None,
            persist_blocked=None, safe_error_detail=lambda value, _limit: value,
            executor_saturated_error=RuntimeError, record_provider_success=None,
            record_provider_failure=None, record_provider_api_capability=None,
        )
        base.update(overrides)
        return AdjustmentFactorMaintenanceDependencies(**base)

    async def _lane(self, outcomes, pending=(date(2026, 9, 17),)):
        """One real ``post_close_sync`` payload, with the provider replaced."""
        database = _Database([{"trading_date": value} for value in pending])
        import app.adjustment_factor_maintenance as module

        async def fake_sync(trade_date, **_kwargs):
            return dict(outcomes[str(trade_date)], trade_date=str(trade_date))

        original = module.sync_daily_controls
        module.sync_daily_controls = fake_sync
        try:
            return await post_close_sync(self._dependencies(database), today=date(2026, 9, 19))
        finally:
            module.sync_daily_controls = original

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


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class BlockedDateLedgerPostgresTests(unittest.TestCase):
    """The ledger SQL itself, on real PostgreSQL, on a fixture-only key.

    ``jsonb_build_object`` concatenation, ``= ANY(%s)`` over a text array and
    the automation_runs status CHECK are all things a fake connection cannot
    prove; the counter is the whole point of the retirement rule.
    """

    trade_date = date(2099, 4, 2)

    def _cleanup(self, connection) -> None:
        connection.execute(
            "DELETE FROM quant.automation_runs WHERE run_key=%s",
            (blocked_date_run_key(self.trade_date),))
        connection.execute(
            "DELETE FROM quant.data_quality_issues WHERE code='adjustment_factor_date_retired' "
            "AND trading_date=%s", (self.trade_date,))

    def test_the_counter_retires_once_and_a_success_clears_it(self):
        from app.adjustment_factor_maintenance import clear_blocked_date
        from app.main import db

        with db.transaction() as connection:
            self._cleanup(connection)
            try:
                for expected in range(1, MAX_CONSECUTIVE_BLOCKED_RUNS + 1):
                    state = record_blocked_date(connection, self.trade_date, "thin cross-section")
                    self.assertEqual(state["consecutive_blocked_runs"], expected)
                    self.assertEqual(state["retired"], expected >= MAX_CONSECUTIVE_BLOCKED_RUNS)
                    self.assertEqual(
                        state["retirement_receipt_written"],
                        expected == MAX_CONSECUTIVE_BLOCKED_RUNS,
                        "the retirement receipt must be written on exactly the retiring run")

                self.assertEqual(retired_dates(connection, [self.trade_date]), {self.trade_date})
                receipts = connection.execute(
                    "SELECT count(*)::int AS rows FROM quant.data_quality_issues "
                    "WHERE code='adjustment_factor_date_retired' AND trading_date=%s",
                    (self.trade_date,)).fetchone()["rows"]
                self.assertEqual(receipts, 1)

                # One more blocked run stays silent rather than alerting again.
                again = record_blocked_date(connection, self.trade_date, "thin cross-section")
                self.assertEqual(again["consecutive_blocked_runs"], MAX_CONSECUTIVE_BLOCKED_RUNS + 1)
                self.assertFalse(again["retirement_receipt_written"])

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
                self.assertFalse(row["output_summary"]["retirement_receipt_written"])
            finally:
                self._cleanup(connection)

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
