from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace

from app.longhu_market_service import sync


class LonghuMarketServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_vendor_failure_persists_reason_without_nameerror(self):
        # Regression: the exception bound by ``except ... as error`` is
        # deleted once the except block exits.  ``fail`` closes over it, so
        # it must capture the fields first or a real (non-mocked) database
        # call raises NameError instead of recording the failure.
        calls: list[tuple[str, tuple]] = []

        class Connection:
            def execute(self, statement, params=None):
                calls.append((statement, params))
                return SimpleNamespace(fetchone=lambda: None, fetchall=lambda: [])

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_database(action, *args, **kwargs):
            return action(*args)

        async def run_public(_fn, *_args, **_kwargs):
            raise RuntimeError("longhu vendor request failed")

        result = await sync(
            date(2026, 8, 21), db=Database(), run_public_blocking=run_public,
            run_database_blocking=run_database, persist_rows=lambda *_args: 0,
            persist_flow_rows=lambda *_args: 0,
            source_factory=lambda: SimpleNamespace(fetch_full_market_evidence=lambda *_args, **_kwargs: None),
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("longhu vendor request failed", result["reason"])
        failure_updates = [
            params for statement, params in calls
            if "error_class=%s,error_message=%s" in statement
        ]
        self.assertEqual(len(failure_updates), 1)
        self.assertEqual(failure_updates[0][0], "RuntimeError")
        self.assertEqual(failure_updates[0][1], "longhu vendor request failed")


class LonghuMarketServiceRosterTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_request_roster_is_the_owner_universe_not_the_vendor_plates(self):
        # The vendor's industry plates are its classification, not its listing
        # roster.  Driving the fetch from them left 255 of 345 BSE names never
        # requested at all, and the exchange sat at ~29 bars a day from
        # 2026-09-04.  The roster must come from quant.universe_members.
        universe = ["600664.SH", "920002.BJ", "920003.BJ"]
        statements: list[str] = []
        received: dict[str, object] = {}

        class Connection:
            def execute(self, statement, params=None):
                statements.append(" ".join(str(statement).split()))
                return SimpleNamespace(
                    fetchone=lambda: None,
                    fetchall=lambda: [{"symbol": symbol} for symbol in universe],
                )

        class Database:
            def transaction(self):
                class Context:
                    def __enter__(self): return Connection()
                    def __exit__(self, *_args): return False
                return Context()

        async def run_database(action, *args, **kwargs):
            return action(*args)

        async def run_public(fn, *args, **_kwargs):
            return fn(*args)

        def fetch_full_market_evidence(trade_date, extra_symbols=()):
            received["trade_date"] = trade_date
            received["extra_symbols"] = list(extra_symbols)
            raise RuntimeError("stop after the roster is handed over")

        result = await sync(
            date(2026, 9, 18), db=Database(), run_public_blocking=run_public,
            run_database_blocking=run_database, persist_rows=lambda *_args: 0,
            persist_flow_rows=lambda *_args: 0,
            source_factory=lambda: SimpleNamespace(
                fetch_full_market_evidence=fetch_full_market_evidence),
        )

        self.assertEqual(received["extra_symbols"], universe)
        self.assertEqual(received["trade_date"], date(2026, 9, 18))
        self.assertTrue(any(
            "quant.universe_members" in statement and "universe_key='all_a'" in statement
            for statement in statements), statements)
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
