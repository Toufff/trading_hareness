"""Focused native-async repository regression coverage."""

from async_database_test_support import *  # noqa: F403


class AsyncIntradayAndAnalystReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_intraday_evidence_lists_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "intraday_watchlists" in sql:
                    return Result(rows=[{"symbol": "600176.SH"}])
                if "intraday_scan_runs" in sql:
                    return Result(row={"scan_id": "scan-1"})
                if "intraday_signal_events WHERE" in sql:
                    return Result(rows=[{"signal_event_id": "signal-1"}])
                return Result(rows=[{"delivery_id": "delivery-1"}])

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        self.assertEqual((await async_watchlists(database))["items"][0]["symbol"], "600176.SH")
        payload = await async_latest_intraday_scan(database, limit=10_000)
        self.assertEqual(payload["scan"]["scan_id"], "scan-1")
        self.assertEqual(payload["signals"][0]["signal_event_id"], "signal-1")
        self.assertEqual(payload["deliveries"][0]["delivery_id"], "delivery-1")
        self.assertEqual(database.connection.calls[-2][1], ("scan-1", 200))
        self.assertEqual(database.connection.calls[-1][1], ("scan-1", 200))

    async def test_intraday_outcome_projection_uses_native_async_connection(self) -> None:
        observed_at = __import__("datetime").datetime(2026, 8, 14, 2, 0, tzinfo=__import__("datetime").timezone.utc)

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                if "JOIN quant.intraday_signal_events" in sql:
                    return Result([{
                        "signal_event_id": "event-1", "symbol": "000001.SZ", "signal_key": "entry-v1",
                        "signal_type": "entry", "observed_at": observed_at, "conditions": {}, "evidence": {},
                    }])
                if "FROM quant.intraday_board_reports" in sql:
                    return Result([])
                return Result([{"horizon_key": "30m", "status": "matured", "rows": 1}])

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        payload = await async_latest_intraday_outcomes(
            database, 1000,
            market_context_from_board_report_fn=lambda *_args: {"status": "available"},
            attribution_fn=lambda *_args: {"stage": "generic"},
            attribution_summary_fn=lambda _rows: {"items": [], "validation_gate": {"status": "accumulating"}},
        )
        self.assertEqual(payload["items"][0]["attribution"]["stage"], "generic")
        self.assertEqual(payload["summary"][0]["rows"], 1)
        self.assertEqual(len(database.connection.calls), 3)

    async def test_catalog_and_market_result_projections_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, rows=None):
                self.rows = rows or []
            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                return Result([{"factor_key": "momentum"}] if "factor_registry" in sql else [{"status": "completed"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        db = Database()
        self.assertEqual((await async_factor_registry(db))["items"][0]["factor_key"], "momentum")
        self.assertEqual((await async_market_snapshots(db, 10))["items"][0]["status"], "completed")
        self.assertEqual(len(db.connection.calls), 2)

    async def test_strategy_projection_uses_native_async_execute_and_fetch(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row = row
                self.rows = rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class CursorConnection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "recommendation_runs" in sql:
                    return Result({"run_id": "run-1", "model_version": "model"})
                return Result(rows=[{"symbol": "600000.SH", "rank": 1}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = CursorConnection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        payload = await latest_strategy_decision(database, "model")
        self.assertEqual(payload["run"]["run_id"], "run-1")
        self.assertEqual(payload["recommendations"][0]["symbol"], "600000.SH")
        self.assertEqual(len(database.connection.calls), 2)
        self.assertIn("recommendations", database.connection.calls[1][0])

    async def test_intraday_status_router_prefers_async_projection_when_configured(self) -> None:
        calls = []

        async def async_status():
            calls.append("async")
            return {"summary": {"states": {"standby": 1}}}

        router = build_intraday_status_router(lambda: {"summary": {"states": {"ready": 1}}}, async_status)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/intraday/services/status")
        payload = await endpoint()
        self.assertEqual(payload["summary"]["states"], {"standby": 1})
        self.assertEqual(calls, ["async"])

    async def test_analyst_skill_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def async_profiles(_database, analyst_id, limit):
            calls.append((analyst_id, limit))
            return {"items": [{"remote_analyst_id": "anqiang"}], "model_version": "test"}

        router = build_analyst_skill_reads_router(
            object(), lambda *_args: {"items": []}, async_database=object(), async_profiles_fn=async_profiles,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-skills")
        payload = await endpoint("anqiang", 7)
        self.assertEqual(payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(calls, [("anqiang", 7)])

    async def test_analyst_skill_projection_uses_native_async_connection(self) -> None:
        class Result:
            async def fetchall(self):
                return [{"remote_analyst_id": "anqiang", "profile": {}}]

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result()

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        payload = await async_analyst_skill_profiles(database, "anqiang", 500)
        self.assertEqual(payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(database.connection.calls[0][1], ("anqiang", 100))

    async def test_analyst_research_router_prefers_async_local_evidence(self) -> None:
        calls = []

        async def async_profiles(_database):
            calls.append("profiles")
            return {"items": [{"remote_analyst_id": "anqiang"}]}

        async def async_observations(_database, analyst_id, limit):
            calls.append((analyst_id, limit))
            return {"items": [{"analyst_id": analyst_id}], "health": []}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(),
            async_profiles_fn=async_profiles, async_observations_fn=async_observations,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        profile_payload = await endpoints["/api/v1/analyst-research/profiles"]()
        observation_payload = await endpoints["/api/v1/analyst-research/observations"]("anqiang", 9)
        self.assertEqual(profile_payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(observation_payload["items"][0]["analyst_id"], "anqiang")
        self.assertEqual(calls, ["profiles", ("anqiang", 9)])

    async def test_analyst_research_projections_use_native_async_connection(self) -> None:
        class Result:
            def __init__(self, rows):
                self.rows = rows

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "remote_analysts" in sql:
                    return Result([{"remote_analyst_id": "anqiang"}])
                if "FROM quant.analyst_observations" in sql and "GROUP BY" not in sql:
                    return Result([{"analyst_id": "anqiang", "observation_id": "o-1"}])
                return Result([{"analyst_id": "anqiang", "observations": 1}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        profile_payload = await async_analyst_research_profiles(database)
        observation_payload = await async_analyst_observations(database, "anqiang", 900)
        self.assertEqual(profile_payload["items"][0]["remote_analyst_id"], "anqiang")
        self.assertEqual(observation_payload["health"][0]["observations"], 1)
        self.assertEqual(database.connection.calls[-2][1], ("anqiang", "anqiang", 500))

    async def test_analyst_archive_router_prefers_async_text_only_pages(self) -> None:
        calls = []

        async def reports(_database, limit, offset):
            calls.append(("reports", limit, offset))
            return {"items": [], "total": 0}

        async def messages(_database, analyst_id, limit, offset):
            calls.append(("messages", analyst_id, limit, offset))
            return {"items": [], "total": 0}

        async def claims(_database, limit, offset):
            calls.append(("claims", limit, offset))
            return {"items": [], "total": 0}

        async def review(_database, status, limit):
            calls.append(("review", status, limit))
            return {"items": [], "status": status}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(),
            async_remote_reports_fn=reports, async_remote_messages_fn=messages,
            async_analyst_claims_fn=claims, async_claim_review_queue_fn=review,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        await endpoints["/api/v1/remote-archive/reports"](7, 2)
        await endpoints["/api/v1/remote-archive/messages"]("anqiang", 8, 3)
        await endpoints["/api/v1/analyst-claims"](9, 4)
        await endpoints["/api/v1/claim-review"]("approved", 10)
        self.assertEqual(calls, [
            ("reports", 7, 2), ("messages", "anqiang", 8, 3),
            ("claims", 9, 4), ("review", "approved", 10),
        ])

    async def test_analyst_archive_pagination_is_bounded_in_native_async_repository(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []

            async def fetchone(self):
                return self.row

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "count(*)" in sql:
                    return Result({"total": 1})
                return Result(rows=[{"remote_report_id": "r-1"}])

        class Transaction:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, *_args):
                return False

        class Database:
            def __init__(self):
                self.connection = Connection()

            def transaction(self):
                return Transaction(self.connection)

        database = Database()
        reports = await async_remote_reports(database, 1000, -2)
        messages = await async_remote_messages(database, "anqiang", 1000, -3)
        self.assertEqual(reports["limit"], 100)
        self.assertEqual(reports["offset"], 0)
        self.assertEqual(messages["limit"], 100)
        self.assertEqual(messages["offset"], 0)
        self.assertEqual(database.connection.calls[0][1], (100, 0))
        self.assertEqual(database.connection.calls[2][1], ("anqiang", "anqiang", 100, 0))
