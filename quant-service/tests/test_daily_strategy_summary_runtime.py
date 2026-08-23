from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timezone
import unittest

from app.daily_strategy_summary_runtime import (
    DailyStrategySummaryRuntimeDependencies,
    run_daily_strategy_summary,
    run_daily_strategy_summary_loop,
)


class _Connection:
    def __init__(self) -> None:
        self.executed = []

    def execute(self, query, params):
        self.executed.append((query, params))


class _Database:
    def __init__(self) -> None:
        self.connection = _Connection()

    @contextmanager
    def transaction(self):
        yield self.connection


class DailyStrategySummaryRuntimeTests(unittest.TestCase):
    def test_summary_is_persisted_as_frontend_only_suppressed_receipt(self) -> None:
        database = _Database()
        calls = []

        async def run_database(operation, *args, **kwargs):
            calls.append((operation.__name__, kwargs.get("timeout_seconds")))
            return operation(*args)

        async def calendar_open(_):
            return True

        async def scheduler(_):
            return None

        dependencies = DailyStrategySummaryRuntimeDependencies(
            database=database, run_database=run_database,
            build_summary=lambda exchange_date: {"exchange_date": str(exchange_date), "signal_counts": {"alerted": 1}},
            summary_text=lambda summary, dashboard_url: f"{summary['exchange_date']} {dashboard_url}",
            dashboard_url=lambda: "https://dashboard.example",
            json_safe=lambda value: value, json_value=lambda value: {"payload": value},
            terminal_for_exchange_date=lambda *_: False, calendar_open=calendar_open,
            now=lambda: datetime(2026, 8, 21, 19, 15, tzinfo=timezone.utc), scheduler=scheduler,
        )
        result = asyncio.run(run_daily_strategy_summary(date(2026, 8, 21), dependencies))

        self.assertEqual(result["status"], "suppressed")
        self.assertIn("Feishu is reserved", result["reason"])
        self.assertEqual(calls, [("<lambda>", None), ("persist_frontend_only", None)])
        query, params = database.connection.executed[0]
        self.assertIn("delivery_status='suppressed'", query)
        self.assertEqual(params[0], date(2026, 8, 21))
        self.assertEqual(params[1], {"payload": result["summary"]})
        self.assertEqual(params[2], "2026-08-21 https://dashboard.example")

    def test_scheduler_adapter_uses_same_date_terminal_receipt(self) -> None:
        database = _Database()
        exchange_date = date(2026, 8, 21)
        calls = []

        async def run_database(operation, *args, **kwargs):
            calls.append((operation.__name__, kwargs.get("timeout_seconds")))
            return operation(*args)

        async def calendar_open(_):
            return True

        async def scheduler(schedule):
            self.assertTrue(await schedule.terminal_for_date(exchange_date))
            self.assertEqual(schedule.run_summary.__name__, "<lambda>")

        dependencies = DailyStrategySummaryRuntimeDependencies(
            database=database, run_database=run_database,
            build_summary=lambda _: {"signal_counts": {}}, summary_text=lambda *_: "summary",
            dashboard_url=lambda: None, json_safe=lambda value: value, json_value=lambda value: value,
            terminal_for_exchange_date=lambda connection, received_date: connection is database.connection and received_date == exchange_date,
            calendar_open=calendar_open,
            now=lambda: datetime(2026, 8, 21, 19, 15, tzinfo=timezone.utc), scheduler=scheduler,
        )
        asyncio.run(run_daily_strategy_summary_loop(dependencies))
        self.assertEqual(calls, [("load", 10)])


if __name__ == "__main__":
    unittest.main()
