"""Coverage for the out-of-band cumulative adjustment-factor repair lane.

The job exists because the settled cross-section and the corporate-action
factor now come from different providers: a trading date can land with
complete bars and limits while its factors are still missing.  It runs in the
04:00-08:00 maintenance window, never inside the post-close pipeline.
"""

from __future__ import annotations

from datetime import date
import unittest

from app.adjustment_factor_maintenance import (
    AdjustmentFactorMaintenanceDependencies,
    PENDING_COVERAGE_RATIO,
    china_today,
    pending_dates,
    pending_dates_between,
    sync,
)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))
        return _Result(self.rows)


class _Database:
    def __init__(self, rows):
        self.connection = _Connection(rows)

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
        # carry identity placeholders is still reported as pending.
        self.assertIn("provider LIKE 'tushare%%'", sql)  # psycopg literal-percent escape
        self.assertIn("coalesce(raw->>'factor_semantics','') <> 'same_day_identity_only'", sql)

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

    async def test_one_failing_date_does_not_end_the_run(self):
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

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_dates"], 1)
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(result["results"][0]["trade_date"], "2026-09-17")

    async def test_nothing_pending_is_reported_as_unchanged(self):
        result = await sync(self._dependencies(_Database([])), today=date(2026, 9, 19))
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["pending_dates"], [])


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
