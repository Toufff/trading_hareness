from __future__ import annotations

from datetime import date
import unittest

from app.stock_study_readiness_repository import stock_window_readiness


class StockStudyReadinessRepositoryTests(unittest.TestCase):
    def test_window_readiness_is_local_and_keeps_missing_p0_as_blocker(self) -> None:
        class Result:
            def __init__(self, row): self.row = row
            def fetchone(self): return self.row

        class Connection:
            def __init__(self): self.calls = []
            def execute(self, sql, params):
                self.calls.append((sql, params))
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

        database = Database()
        result = stock_window_readiness(database, "600000.SH", date(2026, 8, 1), date(2026, 8, 21))
        self.assertFalse(result["decision_ready"])
        self.assertEqual(result["blockers"], ["daily_basic"])
        self.assertEqual(len(result["items"]), 10)
        self.assertTrue(all("FROM quant." in sql for sql, _ in database.connection.calls))


if __name__ == "__main__":
    unittest.main()
