"""Focused native-async repository regression coverage."""

from async_database_test_support import *  # noqa: F403


class AsyncBoardAndSectorReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_curve_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def curves(_database, trade_date, taxonomy, since, **kwargs):
            calls.append((trade_date, taxonomy, since, kwargs))
            return {"items": [], "taxonomy": taxonomy}

        async def review(_database):
            calls.append("review")
            return {"report": None}

        router = build_board_curve_reads_router(
            object(), lambda: 60, lambda: 60, async_database=object(),
            async_curves_fn=curves, async_latest_review_fn=review,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        payload = await endpoints["/api/v1/market/sectors/intraday/curves"](None, "concept", None)
        review_payload = await endpoints["/api/v1/market/sectors/review/report/latest"]()
        self.assertEqual(payload["taxonomy"], "concept")
        self.assertIsNone(review_payload["report"])
        self.assertEqual(calls[0][1], "concept")
        self.assertEqual(calls[0][3], {"curve_retention_days": 60, "rotation_retention_days": 60})
        self.assertEqual(calls[1], "review")

    async def test_board_curve_repository_uses_native_async_rows_and_shared_projection(self) -> None:
        observed_at = datetime(2026, 8, 10, 1, 21, tzinfo=timezone.utc)

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
                if "board_reports ORDER BY" in sql:
                    return Result({"board_report_id": "report-1"})
                if "intraday_board_flow_snapshots" in sql:
                    return Result(rows=[{
                        "observed_at": observed_at, "status": "completed",
                        "coverage": {"concept": {"flow_boards": 1}},
                        "payload": {"items": [{
                            "taxonomy_key": "eastmoney_concept", "sector_key": "BK1", "label": "芯片",
                            "net_inflow": 2.5, "change_pct": 1.0,
                        }]}, "source": "minute_curve",
                    }])
                return Result(rows=[])

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
        review = await async_latest_board_review(database)
        curves = await async_board_flow_curves(
            database, date(2026, 8, 10), "concept", None,
            curve_retention_days=60, rotation_retention_days=60, now=observed_at,
        )
        self.assertEqual(review["report"]["board_report_id"], "report-1")
        self.assertEqual(curves["items"][0]["label"], "芯片")
        self.assertEqual(len(database.connection.calls), 3)

    async def test_board_rotation_and_mining_routers_prefer_async_evidence(self) -> None:
        calls = []

        async def rotations(_database, limit):
            calls.append(("rotations", limit))
            return {"items": [{"rotation_event_id": "event-1"}]}

        async def mining(_database, limit):
            calls.append(("mining", limit))
            return {"run": {"mining_run_id": "run-1"}, "inflow": [], "outflow": []}

        rotation_router = build_board_rotation_reads_router(object(), async_database=object(), async_events_fn=rotations)
        mining_router = build_board_stock_mining_reads_router(object(), async_database=object(), async_mining_fn=mining)
        rotation_endpoint = rotation_router.routes[0].endpoint
        mining_endpoint = mining_router.routes[0].endpoint
        self.assertEqual((await rotation_endpoint(101))["items"][0]["rotation_event_id"], "event-1")
        self.assertEqual((await mining_endpoint(51))["run"]["mining_run_id"], "run-1")
        self.assertEqual(calls, [("rotations", 101), ("mining", 51)])

    async def test_board_rotation_and_mining_repositories_bound_local_rows(self) -> None:
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
                if "mining_runs" in sql:
                    return Result({"mining_run_id": "run-1"})
                if "mining_candidates" in sql:
                    return Result(rows=[
                        {"direction": "inflow", "symbol": "600000.SH"},
                        {"direction": "outflow", "symbol": "000001.SZ"},
                    ])
                return Result(rows=[{"rotation_event_id": "event-1"}])

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
        rotations = await async_board_rotations(database, 1000)
        mining = await async_board_stock_mining(database, 1000)
        self.assertEqual(rotations["items"][0]["rotation_event_id"], "event-1")
        self.assertEqual(mining["inflow"][0]["symbol"], "600000.SH")
        self.assertEqual(mining["outflow"][0]["symbol"], "000001.SZ")
        self.assertEqual(database.connection.calls[0][1], (100,))

    async def test_analyst_action_routers_prefer_async_persisted_evidence(self) -> None:
        calls = []

        async def replay(_database, as_of_date, limit):
            calls.append(("replay", as_of_date, limit))
            return {"items": [], "limit": limit}

        async def outcomes(_database):
            calls.append(("outcomes",))
            return {"outcomes": []}

        action_router = build_analyst_trade_action_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_replay_fn=replay,
        )
        outcome_router = build_analyst_action_outcomes_router(
            object(), lambda *_args, **_kwargs: {}, async_database=object(), async_outcomes_fn=outcomes,
        )
        action = await action_router.routes[0].endpoint(date(2026, 8, 10), 201)
        outcome = await outcome_router.routes[0].endpoint()
        self.assertEqual(action["limit"], 201)
        self.assertEqual(outcome["outcomes"], [])
        self.assertEqual(calls, [("replay", date(2026, 8, 10), 201), ("outcomes",)])

    async def test_analyst_action_repositories_use_native_async_local_evidence(self) -> None:
        stated_at = datetime(2026, 8, 10, 2, tzinfo=timezone.utc)

        class Result:
            def __init__(self, rows=None):
                self.rows = rows or []

            async def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self):
                self.calls = []

            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "analyst_action_intraday_outcomes" in sql:
                    return Result([{"methodology_version": "v1", "count": 1}])
                return Result([{
                    "action_id": "action-1", "stated_at": stated_at, "available_at": stated_at,
                    "quote_price": 10.0, "session_close_price": 10.2, "daily_close": 10.2,
                }])

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
        replay = await async_action_replay(database, date(2026, 8, 10), 1000)
        outcomes = await async_action_outcomes(database)
        self.assertEqual(replay["items"][0]["evaluation_quality"], "persisted_intraday_quote")
        self.assertTrue(replay["items"][0]["factor_eligible"])
        self.assertEqual(outcomes["outcomes"][0]["count"], 1)
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), 200))

    async def test_automation_run_router_prefers_async_receipts(self) -> None:
        calls = []

        async def latest(_database, task_key, limit):
            calls.append((task_key, limit))
            return [{"task_key": task_key, "status": "completed"}]

        router = build_automation_reads_router(object(), async_database=object(), async_latest_runs_fn=latest)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/automation/runs")
        payload = await endpoint("post_close", 1000)
        self.assertEqual(payload["items"][0]["status"], "completed")
        self.assertEqual(calls, [("post_close", 100)])

    async def test_automation_run_repository_uses_native_async_bounded_query(self) -> None:
        class Result:
            async def fetchall(self):
                return [{"run_id": "run-1", "status": "completed"}]

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
        rows = await async_automation_runs(database, "post_close", 1000)
        self.assertEqual(rows[0]["run_id"], "run-1")
        self.assertEqual(database.connection.calls[0][1], ("post_close", "post_close", 100))

    async def test_market_flow_router_prefers_async_persisted_projection(self) -> None:
        calls = []

        async def features(_database, trade_date, *, limit):
            calls.append((trade_date, limit))
            return {"trade_date": str(trade_date), "items": []}

        router = build_market_flow_reads_router(object(), async_database=object(), async_features_fn=features)
        endpoint = router.routes[0].endpoint
        payload = await endpoint(date(2026, 8, 10), 1001)
        self.assertEqual(payload["trade_date"], "2026-08-10")
        self.assertEqual(calls, [(date(2026, 8, 10), 1001)])

    async def test_market_flow_repository_uses_native_async_rows_and_research_gate(self) -> None:
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
                if "DISTINCT ON(exchange_date)" in sql:
                    return Result(rows=[])
                if "sector_flow_daily_features feature" in sql:
                    return Result(rows=[])
                if "sector_flow_daily_outcomes" in sql and "GROUP BY" in sql:
                    return Result(rows=[])
                if "matured_events" in sql:
                    return Result({"trading_days": 59, "matured_events": 199})
                return Result(rows=[{"market_state": "rotation", "feature_key": "minute-1"}])

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
        payload = await async_market_flow_features(database, date(2026, 8, 10), limit=5000)
        self.assertEqual(payload["items"][0]["feature_key"], "minute-1")
        self.assertEqual(payload["research_gate"]["status"], "accumulating")
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), 1000))

    async def test_sector_router_prefers_async_exact_evidence_projections(self) -> None:
        calls = []

        async def backfill(_database, trade_date, **kwargs):
            calls.append(("backfill", trade_date, kwargs))
            return {"states": []}

        async def concepts(_database, trade_date, limit):
            calls.append(("concepts", trade_date, limit))
            return {"items": []}

        async def candidates(_database, trade_date, limit):
            calls.append(("candidates", trade_date, limit))
            return {"items": []}

        async def flows(_database, taxonomy, trade_date, limit):
            calls.append(("flows", taxonomy, trade_date, limit))
            return {"items": []}

        async def sectors(_database, taxonomy, limit, offset):
            calls.append(("sectors", taxonomy, limit, offset))
            return {"items": []}

        async def members(_database, sector_key, taxonomy, limit, offset):
            calls.append(("members", sector_key, taxonomy, limit, offset))
            return {"items": []}

        router = build_sector_reads_router(
            object(), lambda: True, lambda: 25, async_database=object(),
            async_backfill_status_fn=backfill, async_concepts_fn=concepts, async_candidates_fn=candidates,
            async_flows_fn=flows, async_sectors_fn=sectors, async_members_fn=members,
        )
        endpoints = {route.path: route.endpoint for route in router.routes}
        await endpoints["/api/v1/market/sectors/concepts/members/backfill/status"](date(2026, 8, 10))
        await endpoints["/api/v1/market/sectors/concepts"](None, 500)
        await endpoints["/api/v1/market/sectors/concepts/candidates"](None, 100)
        await endpoints["/api/v1/market/sectors/flows"]("ths_industry", None, 100)
        await endpoints["/api/v1/market/sectors"]("ths_index_n", 500, 0)
        await endpoints["/api/v1/market/sectors/{sector_key}/members"]("885001", "ths_index_n", 500, 0)
        self.assertEqual(calls[0], ("backfill", date(2026, 8, 10), {"automatic_enabled": True, "batch_size": 25}))
        self.assertEqual([call[0] for call in calls[1:]], ["concepts", "candidates", "flows", "sectors", "members"])
