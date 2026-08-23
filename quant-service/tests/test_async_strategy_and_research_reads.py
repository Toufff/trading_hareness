"""Focused native-async repository regression coverage."""

from async_database_test_support import *  # noqa: F403


class AsyncStrategyAndResearchReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_sector_repositories_use_native_async_bounds_and_shared_scoring(self) -> None:
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
                if "percent_rank" in sql:
                    return Result(rows=[{
                        "flow_percentile": 1.0, "change_pct": 2.0, "up_nums": None,
                        "streak_days": None, "raw": {}, "strength_raw": {}, "label": "芯片",
                    }])
                if "count(m.symbol)" in sql:
                    return Result(rows=[{"sector_key": "885001", "label": "半导体"}])
                if "count(*)::int total FROM quant.sectors" in sql:
                    return Result({"total": 2})
                if "sector_membership_history m JOIN" in sql:
                    return Result(rows=[{"symbol": "600000.SH"}])
                if "effective_to IS NULL" in sql:
                    return Result({"total": 3})
                return Result({"latest": date(2026, 8, 10)})

        class Transaction:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Transaction(self.connection)

        database = Database()
        concepts = await async_concept_signals(database, date(2026, 8, 10), 10000)
        sectors = await async_market_sectors(database, "ths_index_n", 10000, -1)
        members = await async_sector_members(database, "885001", "ths_index_n", 10000, -1)
        self.assertEqual(concepts["items"][0]["aggregate_score"], 86.0)
        self.assertEqual(sectors["limit"], 1000)
        self.assertEqual(members["total"], 3)
        self.assertEqual(database.connection.calls[0][1], (date(2026, 8, 10), date(2026, 8, 10), 1000))

    def test_concept_mapping_status_separates_active_exact_coverage_from_receipts(self) -> None:
        payload = project_concept_member_backfill_status(
            date(2026, 8, 21), 387, 0,
            {"mapped_concepts": 387, "member_rows": 70998, "latest_available_at": "2026-08-21T12:40:26+00:00"}, [],
            automatic_enabled=False, batch_size=25,
        )
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["receipt_complete"])
        self.assertEqual(payload["mapped_concepts"], 387)
        self.assertEqual(payload["receipt_mapped_concepts"], 0)
        self.assertEqual(payload["states"][0]["state"], "active_exact_mapping")

    async def test_limit_linkage_router_prefers_async_exact_relation_evidence(self) -> None:
        calls = []

        async def linkage(_database, limit):
            calls.append(limit)
            return {"items": [{"symbol": "600000.SH"}]}

        router = build_limit_linkage_mining_reads_router(
            object(), async_database=object(), async_linkage_fn=linkage,
        )
        payload = await router.routes[0].endpoint(51)
        self.assertEqual(payload["items"][0]["symbol"], "600000.SH")
        self.assertEqual(calls, [51])

    async def test_limit_linkage_repository_uses_native_async_bounded_rows(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "mining_runs" in sql:
                    return Result({"linkage_run_id": "run-1"})
                return Result(rows=[{"symbol": "600000.SH"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_limit_linkage_mining(database, 1000)
        self.assertEqual(payload["items"][0]["symbol"], "600000.SH")
        self.assertEqual(database.connection.calls[1][1], ("run-1", 50))

    async def test_prompt_lab_status_router_prefers_async_research_only_projection(self) -> None:
        calls = []

        async def status(_database, limit):
            calls.append(limit)
            return {"candidates": [], "live_effect": "none"}

        router = build_analyst_prompt_lab_router(
            object(), lambda *_args, **_kwargs: {}, lambda *_args, **_kwargs: {},
            lambda *_args, **_kwargs: {}, lambda *_args, **_kwargs: {},
            async_database=object(), async_status_fn=status,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-prompt-lab/status")
        payload = await endpoint(1000)
        self.assertEqual(payload["live_effect"], "none")
        self.assertEqual(calls, [1000])

    async def test_prompt_lab_status_uses_native_async_bounded_evidence(self) -> None:
        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "prompt_candidates" in sql: return Result([{"candidate_id": "candidate-1"}])
                if "evaluation_runs" in sql: return Result([{"evaluation_id": "evaluation-1"}])
                return Result([{"methodology_version": "v1", "count": 1}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_prompt_lab_status(database, 1000)
        self.assertEqual(payload["candidates"][0]["candidate_id"], "candidate-1")
        self.assertEqual(payload["evaluations"][0]["evaluation_id"], "evaluation-1")
        self.assertEqual(database.connection.calls[0][1], (500,))

    async def test_analyst_review_reads_prefer_native_async_bounded_evidence(self) -> None:
        calls = []

        async def reviews(_database, cadence, limit):
            calls.append((cadence, limit))
            return {"items": [{"review_id": "review-1"}], "live_effect": "none"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_list_reviews_fn=reviews,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/reviews")
        payload = await endpoint("daily", 1000)
        self.assertEqual(payload["items"][0]["review_id"], "review-1")
        self.assertEqual(calls, [("daily", 1000)])

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([{"review_id": "review-1"}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_market_reviews(database, "daily", 1000)
        self.assertEqual(direct["items"][0]["review_id"], "review-1")
        self.assertEqual(database.connection.calls[0][1], ("daily", "daily", 100))

    async def test_analyst_market_evaluation_prefers_native_async_evidence(self) -> None:
        calls = []

        async def evaluation(_database, start, end, analyst):
            calls.append((start, end, analyst))
            return {"quality_gate": {"live_strategy_effect": "none"}}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_market_evaluation_fn=evaluation,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/market-evaluation")
        start, end = date(2026, 8, 1), date(2026, 8, 15)
        payload = await endpoint(start, end, "anqiang-touzi-riji")
        self.assertEqual(payload["quality_gate"]["live_strategy_effect"], "none")
        self.assertEqual(calls, [(start, end, "anqiang-touzi-riji")])

        class Result:
            async def fetchall(self): return []

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_market_evaluation(database, start, end, "anqiang-touzi-riji")
        self.assertEqual(direct["quality_gate"]["live_strategy_effect"], "none")
        self.assertEqual(len(database.connection.calls), 8)
        self.assertEqual(database.connection.calls[0][1], (start, end, "anqiang-touzi-riji", "anqiang-touzi-riji"))

    async def test_analyst_stock_timeline_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def timeline(_database, **kwargs):
            calls.append(kwargs)
            return {"symbol": kwargs["symbol"], "bar_count": 0, "boundary": "no media is fetched"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_stock_timeline_fn=timeline,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/stock-timeline")
        payload = await endpoint("600000.SH", date(2026, 8, 21), date(2026, 8, 21), "anqiang-touzi-riji", 9999)
        self.assertEqual(payload["symbol"], "600000.SH")
        self.assertEqual(calls[0]["limit"], 9999)

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "max(trading_date)" in sql: return Result({"latest_date": date(2026, 8, 21)})
                return Result(rows=[])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_stock_timeline(database, symbol="600000.SH", start_date=date(2026, 8, 21), limit=9999)
        self.assertEqual(direct["bar_count"], 0)
        self.assertEqual(database.connection.calls[1][1][-1], 3000)

    async def test_analyst_research_status_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def status(_database, as_of_date):
            calls.append(as_of_date)
            return {"approved_theme_board_aliases": 1, "boundary": "research-only"}

        router = build_analyst_research_reads_router(
            object(), lambda *_args: {}, async_database=object(), async_status_fn=status,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-research/status")
        as_of = date(2026, 8, 21)
        payload = await endpoint(as_of)
        self.assertEqual(payload["approved_theme_board_aliases"], 1)
        self.assertEqual(calls, [as_of])

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "count(*)" in sql: return Result({"count": 2})
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_research_status(database, as_of)
        self.assertEqual(direct["approved_theme_board_aliases"], 2)
        self.assertEqual(len(database.connection.calls), 5)

    async def test_analyst_archive_state_and_cursor_prefer_native_async_local_evidence(self) -> None:
        calls = []

        async def state(_database):
            calls.append("state")
            return {"analysts": [{"remote_analyst_id": "anqiang-touzi-riji"}]}

        async def cursor(_database, stream_key, analyst_id):
            calls.append((stream_key, analyst_id))
            return {"stream_key": stream_key, "remote_analyst_id": analyst_id, "cursor": {}}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(),
            async_remote_archive_state_fn=state, async_sync_cursor_fn=cursor,
        )
        state_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/remote-archive/state")
        cursor_endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/remote-archive/sync-cursors/{stream_key}/{analyst_id}")
        self.assertEqual((await state_endpoint())["analysts"][0]["remote_analyst_id"], "anqiang-touzi-riji")
        self.assertEqual((await cursor_endpoint("messages", "anqiang-touzi-riji"))["stream_key"], "messages")
        self.assertEqual(calls, ["state", ("messages", "anqiang-touzi-riji")])

        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "remote_analysts a" in sql: return Result(rows=[{"remote_analyst_id": "anqiang-touzi-riji"}])
                return Result({"stream_key": "messages", "remote_analyst_id": "anqiang-touzi-riji"})

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        self.assertEqual((await async_archive_state(database))["analysts"][0]["remote_analyst_id"], "anqiang-touzi-riji")
        direct = await async_archive_sync_cursor(database, "messages", "anqiang-touzi-riji")
        self.assertEqual(direct["remote_analyst_id"], "anqiang-touzi-riji")
        self.assertEqual(database.connection.calls[-1][1], ("messages", "anqiang-touzi-riji"))
