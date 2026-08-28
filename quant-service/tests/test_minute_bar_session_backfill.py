"""Coverage for the session-scoped minute-bar backfill.

The pass exists to unblock time-matched benchmarks and entry-timing research.
It is written to survive an upstream that mostly does not answer - ``stk_mins``
served 1 of 18 sampled symbols on 2026-08-26 - and to report that fact rather
than leave it to be discovered in an empty table.
"""

from __future__ import annotations

import asyncio
from datetime import date
import unittest
from unittest.mock import MagicMock, patch

from app.minute_bar_session_backfill import (
    BENCHMARK_SYMBOLS,
    backfill_session,
    coverage_report,
    session_symbols,
)


class SessionSymbolTests(unittest.TestCase):
    def _connection(self, boards):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"trading_date": date(2026, 8, 28), "symbol": symbol} for symbol in boards
        ]
        return connection

    def test_benchmarks_lead_the_list(self):
        result = session_symbols(self._connection(["600000.SH"]), date(2026, 8, 28))
        self.assertEqual(result["symbols"][:len(BENCHMARK_SYMBOLS)], list(BENCHMARK_SYMBOLS))

    def test_a_board_that_is_also_a_benchmark_is_not_requested_twice(self):
        result = session_symbols(self._connection(["000001.SH", "600000.SH"]),
                                 date(2026, 8, 28))
        self.assertEqual(len(result["symbols"]), len(set(result["symbols"])))

    def test_the_cap_truncates_boards_and_says_so(self):
        result = session_symbols(self._connection([f"6000{index:02d}.SH" for index in range(10)]),
                                 date(2026, 8, 28), limit=4)
        self.assertEqual(result["boards"], 10)
        self.assertEqual(result["truncated"], 6)

    def test_the_cap_never_drops_a_benchmark(self):
        # Benchmarks are why a session is comparable at all; a busy board day
        # must not cost them.
        result = session_symbols(self._connection([f"6000{index:02d}.SH" for index in range(10)]),
                                 date(2026, 8, 28), limit=0)
        for symbol in BENCHMARK_SYMBOLS:
            self.assertIn(symbol, result["symbols"])

    def test_a_session_with_no_boards_still_requests_the_benchmarks(self):
        result = session_symbols(self._connection([]), date(2026, 8, 28))
        self.assertEqual(result["symbols"], list(BENCHMARK_SYMBOLS))


class BackfillSessionTests(unittest.TestCase):
    def _run(self, symbols, side_effect):
        with patch("app.minute_bar_session_backfill.backfill_symbol_session",
                   side_effect=side_effect) as backfill:
            result = asyncio.run(backfill_session(
                date(2026, 8, 28), symbols=symbols,
                call_tushare_api=MagicMock(), run_database_blocking=MagicMock(), db=MagicMock()))
        return result, backfill

    def test_one_unavailable_symbol_does_not_end_the_pass(self):
        async def side_effect(symbol, _trading_date, **_kwargs):
            if symbol == "B":
                raise RuntimeError("HTTP 202")
            return {"symbol": symbol, "status": "completed", "bars": 241}

        result, _ = self._run(["A", "B", "C"], side_effect)
        self.assertEqual(result["requested"], 3)
        self.assertEqual(result["status_counts"]["completed"], 2)
        self.assertEqual(result["status_counts"]["failed"], 1)

    def test_a_failure_is_recorded_with_its_cause(self):
        async def side_effect(symbol, _trading_date, **_kwargs):
            raise RuntimeError("HTTP 202 Retry-After 30")

        result, _ = self._run(["A"], side_effect)
        self.assertIn("Retry-After", result["results"][0]["error"])

    def test_symbols_are_requested_one_at_a_time_in_order(self):
        seen = []

        async def side_effect(symbol, _trading_date, **_kwargs):
            seen.append(symbol)
            return {"symbol": symbol, "status": "empty", "bars": 0}

        self._run(["A", "B", "C"], side_effect)
        self.assertEqual(seen, ["A", "B", "C"])


class CoverageReportTests(unittest.TestCase):
    def test_availability_counts_answered_over_requested(self):
        report = coverage_report([
            {"status": "completed", "bars": 241},
            {"status": "partial", "bars": 100},
            {"status": "empty", "bars": 0},
            {"status": "failed", "bars": 0},
        ])
        self.assertEqual(report["answered"], 2)
        self.assertEqual(report["availability_pct"], 50.0)
        self.assertEqual(report["bars"], 341)

    def test_an_empty_response_is_not_counted_as_answered(self):
        # The upstream's 202-and-never-resolve shows up as empty; counting it
        # would hide exactly the fact this report exists to surface.
        report = coverage_report([{"status": "empty", "bars": 0}] * 17
                                 + [{"status": "completed", "bars": 241}])
        self.assertAlmostEqual(report["availability_pct"], 5.56, places=2)

    def test_no_results_reports_no_availability_rather_than_zero(self):
        self.assertIsNone(coverage_report([])["availability_pct"])


if __name__ == "__main__":
    unittest.main()
