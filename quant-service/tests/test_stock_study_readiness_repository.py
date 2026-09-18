from __future__ import annotations

from datetime import date
import unittest

from app.stock_study_readiness_repository import stock_study_claims, stock_window_readiness


class StockStudyReadinessRepositoryTests(unittest.TestCase):
    @staticmethod
    def _database(pending_rows, adjustment=None, ledger_rows=()):
        class Result:
            def __init__(self, row, rows=()): self.row, self.rows = row, list(rows)
            def fetchone(self): return self.row
            def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params):
                self.calls.append((sql, params))
                if sql.lstrip().startswith("SELECT as_of_date"):
                    return Result(None, ledger_rows)
                if "factored.symbol IS NOT NULL" in sql:
                    return Result(None, pending_rows)
                if "uncovered_dates" in sql:
                    return Result(adjustment if adjustment is not None else {
                        "rows": 1, "latest_date": date(2026, 8, 21),
                        "settled_sessions": 1, "uncovered_dates": []})
                if "daily_fundamentals" in sql:
                    return Result({"rows": 0, "latest_date": None})
                return Result({"rows": 1, "latest_date": date(2026, 8, 21)})

        class Tx:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        return Database()

    def test_window_readiness_is_local_and_keeps_missing_p0_as_blocker(self) -> None:
        database = self._database([])
        result = stock_window_readiness(database, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))
        self.assertFalse(result["decision_ready"])
        self.assertEqual(result["blockers"], ["daily_basic"])
        self.assertEqual(len(result["items"]), 10)
        self.assertTrue(all("FROM quant." in sql for sql, _ in database.connection.calls))

    @staticmethod
    def _adjustment_item(database):
        return next(entry for entry in stock_window_readiness(
            database, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))["items"]
            if entry["api_name"] == "adj_factor")

    def test_adjustment_status_is_symbol_scoped_ready_pending_missing(self) -> None:
        # The status is derived from THIS symbol's settled sessions, not from a
        # raw row count: the repair runbook only annotates the placeholder rows
        # and never deletes them, so a bare count(*) > 0 would report "ready"
        # forever on exactly the symbols and dates that are still damaged.
        covered = self._adjustment_item(self._database([]))
        self.assertEqual(covered["status"], "ready")
        self.assertTrue(covered["note"].startswith("complete:"))
        self.assertEqual(covered["priority"], "P1")

        # Uncovered AND on the maintenance work list -> pending.
        pending = self._adjustment_item(self._database(
            [{"trading_date": date(2026, 8, 20)}],
            adjustment={"rows": 3, "latest_date": date(2026, 8, 19), "settled_sessions": 4,
                        "uncovered_dates": [date(2026, 8, 20)]}))
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(pending["pending_dates"], ["2026-08-20"])
        self.assertEqual(pending["missing_dates"], [])
        self.assertTrue(pending["note"].startswith("pending: 1 of 4 settled session"))

        # Uncovered and NOT on the work list -> missing, never a false ready.
        missing = self._adjustment_item(self._database(
            [],
            adjustment={"rows": 3, "latest_date": date(2026, 8, 19), "settled_sessions": 4,
                        "uncovered_dates": [date(2026, 8, 20)]}))
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["missing_dates"], ["2026-08-20"])
        self.assertEqual(missing["rows"], 3)

        # A mixed window fails closed rather than advertising a repair that is
        # not actually queued for every damaged date.
        mixed = self._adjustment_item(self._database(
            [{"trading_date": date(2026, 8, 20)}],
            adjustment={"rows": 2, "latest_date": date(2026, 8, 18), "settled_sessions": 4,
                        "uncovered_dates": [date(2026, 8, 19), date(2026, 8, 20)]}))
        self.assertEqual(mixed["status"], "missing")
        self.assertEqual(mixed["pending_dates"], ["2026-08-20"])
        self.assertEqual(mixed["missing_dates"], ["2026-08-19"])

        # Never fetched at all.
        never = self._adjustment_item(self._database(
            [], adjustment={"rows": 0, "latest_date": None, "settled_sessions": 0,
                            "uncovered_dates": []}))
        self.assertEqual(never["status"], "missing")
        self.assertEqual(never["rows"], 0)

    def test_a_retired_date_is_reported_as_retired_with_its_ledger_reason(self) -> None:
        """A date the factor job gave up on must not read as "queued".

        After MAX_CONSECUTIVE_BLOCKED_RUNS coverage refusals the maintenance
        lane drops the date, so nothing is going to repair it; the label has to
        say so, and say which ledger row an operator clears to re-open it.
        """
        retired = self._adjustment_item(self._database(
            [{"trading_date": date(2026, 8, 20)}],
            adjustment={"rows": 3, "latest_date": date(2026, 8, 19), "settled_sessions": 4,
                        "uncovered_dates": [date(2026, 8, 20)]},
            ledger_rows=[{"as_of_date": date(2026, 8, 20),
                          "run_key": "adjustment-factor-blocked:2026-08-20",
                          "output_summary": {"consecutive_blocked_runs": 5,
                                             "reason": "thin cross-section"}}]))
        self.assertEqual(retired["status"], "retired")
        self.assertEqual(retired["retired_dates"], ["2026-08-20"])
        # It is neither queued nor silently folded into "missing".
        self.assertEqual(retired["pending_dates"], [])
        self.assertEqual(retired["missing_dates"], [])
        self.assertIn("thin cross-section", retired["note"])
        self.assertIn("adjustment-factor-blocked:2026-08-20", retired["note"])
        self.assertIn("no repair is queued", retired["note"])
        self.assertNotIn("queued for the adjustment-factor maintenance job", retired["note"])

    def test_a_retired_date_beside_a_queued_one_still_fails_closed(self) -> None:
        mixed = self._adjustment_item(self._database(
            [{"trading_date": date(2026, 8, 19)}, {"trading_date": date(2026, 8, 20)}],
            adjustment={"rows": 2, "latest_date": date(2026, 8, 18), "settled_sessions": 4,
                        "uncovered_dates": [date(2026, 8, 19), date(2026, 8, 20)]},
            ledger_rows=[{"as_of_date": date(2026, 8, 20),
                          "run_key": "adjustment-factor-blocked:2026-08-20",
                          "output_summary": {"consecutive_blocked_runs": 7,
                                             "reason": "thin cross-section"}}]))
        # The window can never complete, so it is not "pending"; the retired
        # date is named because it is the one an operator has to act on.
        self.assertEqual(mixed["status"], "retired")
        self.assertEqual(mixed["retired_dates"], ["2026-08-20"])
        self.assertEqual(mixed["pending_dates"], ["2026-08-19"])

    def test_the_adjustment_count_excludes_placeholder_evidence(self) -> None:
        database = self._database([])
        stock_window_readiness(database, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))
        sql = next(sql for sql, _ in database.connection.calls if "uncovered_dates" in sql)
        self.assertIn("factor.provider LIKE 'tushare%%'", sql)
        self.assertIn("IN ('','corporate_action_cumulative')", sql)
        # The settled side asks the bar table; only the factored side is
        # allowed to touch quant.daily_adjustment_factors, and only through
        # the shared promotion predicate.
        self.assertNotIn("quant.daily_adjustment_factors", sql.split("factored AS")[0])

    def test_stock_claim_summary_is_text_only_and_does_not_promote_live_weight(self) -> None:
        class Result:
            def fetchall(self):
                return [
                    {"direction": 1, "strength": 0.8, "extraction_confidence": 0.5},
                    {"direction": -1, "strength": 0.4, "extraction_confidence": 0.5},
                ]

        class Connection:
            def execute(self, sql, params):
                self.sql, self.params = sql, params
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            def __enter__(self): return self.connection
            def __exit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        claims, summary = stock_study_claims(Database(), "600000.SH")
        self.assertEqual(len(claims), 2)
        self.assertEqual(summary, {"claim_count": 2, "positive": 1, "negative": 1, "neutral": 0,
                                   "score": 0.3333, "direction": "positive"})


if __name__ == "__main__":
    unittest.main()
