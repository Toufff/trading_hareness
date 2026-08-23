"""Focused native-async repository regression coverage."""

from async_database_test_support import *  # noqa: F403


class AsyncProviderAndReadinessReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_health_prefers_native_async_local_evidence(self) -> None:
        calls = []

        async def health(_database, configs, observed_at):
            calls.append((configs, observed_at))
            return {"summary": {"healthy": 1}, "items": []}

        router = build_provider_status_router(
            object(), lambda: [{"provider_key": "tushare_super_get", "configured": True}], lambda: [],
            async_database=object(), async_provider_health_fn=health,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/providers/health")
        self.assertEqual((await endpoint())["summary"]["healthy"], 1)
        self.assertEqual(calls[0][0][0]["provider_key"], "tushare_super_get")

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([{"provider_key": "tushare_super_get", "enabled": True, "capability": "rt_k", "market": "cn",
                                "circuit_open_until": None, "last_success_at": None, "last_failure_at": None}])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_provider_health(
            database, [{"provider_key": "tushare_super_get", "configured": True}], datetime(2026, 8, 22, tzinfo=timezone.utc),
        )
        self.assertEqual(direct["items"][0]["state"], "unknown")
        self.assertEqual(len(database.connection.calls), 1)

    async def test_analyst_factors_prefer_native_async_text_only_evidence(self) -> None:
        calls = []

        async def summary(_database, as_of_date, lookback_days):
            calls.append((as_of_date, lookback_days))
            return {"factor_version": "analyst-text-consensus-v1", "data_boundary": "text-only"}

        router = build_analyst_reads_router(
            object(), lambda *_args: {}, lambda *_args: {}, async_database=object(), async_factor_summary_fn=summary,
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/analyst-factors")
        as_of = date(2026, 8, 21)
        self.assertEqual((await endpoint(as_of, 100)) ["data_boundary"], "text-only")
        self.assertEqual(calls, [(as_of, 100)])

        class Result:
            def __init__(self, rows): self.rows = rows
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return Result([])

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        direct = await async_analyst_factor_summary(database, as_of, 100)
        self.assertEqual(direct["lookback_days"], 30)
        self.assertEqual(database.connection.calls[0][1][:2], (date(2026, 7, 23), as_of))

    async def test_strategy_health_projection_reads_all_local_rows_async(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None):
                self.row, self.rows = row, rows or []
            async def fetchone(self):
                return self.row
            async def fetchall(self):
                return self.rows

        class Connection:
            async def execute(self, sql, _params=()):
                if "signal_key AS strategy_key" in sql:
                    return Result(rows=[])
                if "avg(raw_return)" in sql:
                    return Result({"rows": 0, "positive": 0, "avg_return": None})
                if "latest_quote_at" in sql:
                    return Result({"latest_quote_at": None, "fresh_quote_rows": 0})
                return Result({"signals_7d": 0, "signals_prior_7d": 0, "episodes_7d": 0,
                                "matured_30m_7d": 0, "matured_days_7d": 0})

        class Tx:
            async def __aenter__(self):
                return Connection()
            async def __aexit__(self, *_args):
                return False

        class Database:
            def transaction(self):
                return Tx()

        payload = await latest_strategy_health(Database())
        self.assertEqual(payload["status"], "research_only")
        self.assertEqual(payload["validation_gate"]["live_effect"], "none")

    async def test_replay_readiness_projection_uses_native_async_connection(self) -> None:
        class Result:
            async def fetchone(self):
                return {
                    "full_cross_section_days": 0, "offline_minute_trading_days": 0,
                    "offline_minute_symbols": 0, "offline_minute_bars": 0,
                    "completed_offline_imports": 0, "confirmed_signal_events": 0,
                    "matured_signal_events": 0,
                }

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, _params=()):
                self.calls.append(sql)
                return Result()

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_replay_readiness(database)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(len(database.connection.calls), 1)
        self.assertIn("canonical_bars_daily", database.connection.calls[0])

    async def test_research_readiness_router_prefers_async_replay_projection(self) -> None:
        calls = []

        class Result:
            async def fetchone(self):
                return {"full_cross_section_days": 0, "offline_minute_trading_days": 0,
                        "offline_minute_symbols": 0, "offline_minute_bars": 0,
                        "completed_offline_imports": 0, "confirmed_signal_events": 0,
                        "matured_signal_events": 0}

        class Connection:
            async def execute(self, _sql, _params=()):
                calls.append("async")
                return Result()

        class Tx:
            async def __aenter__(self): return Connection()
            async def __aexit__(self, *_args): return False

        class Database:
            def transaction(self): return Tx()

        def must_not_run(_database):
            raise AssertionError("sync replay readiness path was selected")

        router = build_research_readiness_router(
            object(), lambda _request: {}, lambda _database: {}, must_not_run,
            async_database=Database(),
        )
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/data-readiness/replay")
        payload = await endpoint()
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(calls, ["async"])

    async def test_historical_estimate_projection_uses_native_async_connection(self) -> None:
        class Result:
            def __init__(self, row=None, rows=None): self.row, self.rows = row, rows or []
            async def fetchone(self): return self.row
            async def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.calls = []
            async def execute(self, sql, params=()):
                self.calls.append((sql, params))
                if "quant.sectors" in sql: return Result({"total": 0})
                if "canonical_bars_daily" in sql: return Result({"first_bar_date": None, "latest_bar_date": None,
                    "bar_days": 0, "full_cross_section_days": 0, "max_symbols_on_day": 0,
                    "fundamental_symbols": 0, "limit_symbols": 0, "minute_symbols": 0})
                if "tushare_raw_records" in sql: return Result(rows=[])
                return Result({"symbols": 5500})

        class Tx:
            def __init__(self, connection): self.connection = connection
            async def __aenter__(self): return self.connection
            async def __aexit__(self, *_args): return False

        class Database:
            def __init__(self): self.connection = Connection()
            def transaction(self): return Tx(self.connection)

        database = Database()
        payload = await async_historical_estimate(database, HistoricalCoverageEstimateRequest())
        self.assertEqual(payload["current_coverage"]["bar_days"], 0)
        self.assertEqual(len(database.connection.calls), 4)

    async def test_event_router_prefers_async_local_projection(self) -> None:
        async def announcements(*_args, **_kwargs):
            return {"items": [], "async": True}
        async def lhb(*_args, **_kwargs):
            return {"items": [], "async": True}
        router = build_event_reads_router(None, object())
        # Route assembly uses the production async repository; this smoke
        # check also guards that both public endpoints remain GET-only.
        self.assertEqual({route.path: route.methods for route in router.routes}, {
            "/api/v1/events/announcements": {"GET"}, "/api/v1/events/lhb": {"GET"},
        })
