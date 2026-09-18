from __future__ import annotations

from datetime import date
import unittest

from app.stock_study_readiness_repository import stock_study_claims, stock_window_readiness


class StockStudyReadinessRepositoryTests(unittest.TestCase):
    @staticmethod
    def _database(pending_rows):
        class Result:
            def __init__(self, row, rows=()): self.row, self.rows = row, list(rows)
            def fetchone(self): return self.row
            def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params):
                self.calls.append((sql, params))
                if "factored.symbol IS NOT NULL" in sql:
                    return Result(None, pending_rows)
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

    def test_adjustment_factor_note_separates_pending_from_missing(self) -> None:
        # A window whose settled dates are queued for the factor maintenance
        # job must say "pending", not the bare "missing" that reads like an
        # unrecoverable data hole -- and must never read "ready" off a
        # placeholder row, which is what the identity factor used to produce.
        pending = self._database([{"trading_date": date(2026, 8, 20)}])
        item = next(entry for entry in stock_window_readiness(
            pending, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))["items"]
            if entry["api_name"] == "adj_factor")
        self.assertTrue(item["note"].startswith("pending: 1 settled trade date"))
        self.assertEqual(item["priority"], "P1")

        complete = self._database([])
        item = next(entry for entry in stock_window_readiness(
            complete, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))["items"]
            if entry["api_name"] == "adj_factor")
        self.assertTrue(item["note"].startswith("complete:"))

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
