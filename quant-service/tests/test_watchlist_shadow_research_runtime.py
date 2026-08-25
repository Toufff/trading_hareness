"""Contract coverage for watchlist shadow-research persistence."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.watchlist_shadow_research_runtime import (
    WatchlistShadowResearchRuntime,
    WatchlistShadowResearchRuntimeDependencies,
)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return None


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, values):
        self.calls.append((statement, values))
        return type("Result", (), {"fetchone": lambda _self: {
            "strategy_experiment_id": f"id-{len(self.calls)}",
            "created_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
        }})()


class WatchlistShadowResearchRuntimeTests(unittest.TestCase):
    def test_persists_main_wave_and_rebound_as_one_transactional_research_receipt(self):
        connection = _Connection()
        database = type("Database", (), {"transaction": lambda _self: _Transaction(connection)})()
        called = []

        def model(key):
            def run(received_connection, as_of_date):
                self.assertIs(received_connection, connection)
                called.append((key, as_of_date))
                return {
                    "strategy_key": key, "status": "completed", "start_date": date(2026, 8, 1),
                    "end_date": date(2026, 8, 22), "parameters": {}, "metrics": {},
                    "equity_curve": [], "trades": [],
                }
            return run

        runtime = WatchlistShadowResearchRuntime(WatchlistShadowResearchRuntimeDependencies(
            database=database, main_wave_research=model("main"), rebound_research=model("rebound"),
            main_wave_key="main", rebound_key="rebound", china_today=lambda: date(2026, 8, 24),
            json_safe=lambda value: value, json_value=lambda value: value,
        ))
        payload = type("Payload", (), {"as_of_date": date(2026, 8, 22)})()

        result = runtime.persist(payload)

        self.assertEqual(called, [("main", date(2026, 8, 22)), ("rebound", date(2026, 8, 22))])
        self.assertEqual(len(connection.calls), 2)
        self.assertEqual(result["strategy_key"], "main")
        self.assertEqual(result["countertrend_rebound"]["strategy_key"], "rebound")


if __name__ == "__main__":
    unittest.main()
