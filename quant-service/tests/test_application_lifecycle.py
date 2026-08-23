from __future__ import annotations

import asyncio
import unittest

from app.application_lifecycle import ApplicationLifecycleDependencies, application_lifespan


class ApplicationLifecycleTests(unittest.TestCase):
    def test_starts_and_stops_local_resources_in_safe_order(self) -> None:
        events: list[str] = []

        def mark(name: str):
            def callback(*_args, **_kwargs):
                events.append(name)
            return callback

        async def async_mark(name: str):
            events.append(name)

        async def request_reserver(*_args, **_kwargs):
            return None

        def configure(reserver, **_kwargs):
            events.append("configure:on" if reserver is not None else "configure:off")

        def ensure_catalog() -> None:
            events.append("catalog")

        async def run_database(operation, *, timeout_seconds: int):
            self.assertIs(operation, ensure_catalog)
            self.assertEqual(timeout_seconds, 30)
            events.append("run_database")
            operation()

        async def start_http():
            await async_mark("http:start")

        async def cancel_tasks(tasks):
            self.assertEqual(tasks, {"loop": object_marker})
            await async_mark("tasks:cancel")

        async def cancel_snapshot():
            await async_mark("snapshot:cancel")

        async def close_http():
            await async_mark("http:close")

        async def close_async_database():
            await async_mark("async_db:close")

        object_marker = object()
        dependencies = ApplicationLifecycleDependencies(
            open_database=mark("db:open"),
            open_async_database=lambda: async_mark("async_db:open"),
            configure_request_reserver=configure,
            request_reserver=request_reserver,
            max_reservation_wait_seconds=3.0,
            initialize_provider_metrics=mark("metrics:init"),
            start_http_clients=start_http,
            legacy_schema_bootstrap_enabled=lambda: True,
            migrate_database=mark("db:migrate"),
            verify_versioned_schema=mark("db:verify"),
            ensure_catalog_capabilities=ensure_catalog,
            run_database=run_database,
            start_background_tasks=lambda: events.append("tasks:start") or {"loop": object_marker},
            cancel_background_tasks=cancel_tasks,
            cancel_shared_snapshots=cancel_snapshot,
            shutdown_super_get_executor=mark("super_get:shutdown"),
            shutdown_runtime_executors=mark("executors:shutdown"),
            close_http_clients=close_http,
            close_async_database=close_async_database,
            close_database=mark("db:close"),
        )

        async def exercise() -> None:
            async with application_lifespan(dependencies):
                events.append("inside")

        asyncio.run(exercise())
        self.assertEqual(events, [
            "db:open", "async_db:open", "configure:on", "metrics:init", "http:start",
            "db:migrate", "db:verify", "run_database", "catalog", "tasks:start", "inside",
            "tasks:cancel", "snapshot:cancel", "super_get:shutdown", "executors:shutdown",
            "http:close", "configure:off", "async_db:close", "db:close",
        ])

    def test_skips_legacy_migration_when_bootstrap_is_disabled(self) -> None:
        events: list[str] = []

        async def nothing_async():
            return None

        async def run_database(*_args, **_kwargs):
            return None

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: None,
            open_async_database=nothing_async,
            configure_request_reserver=lambda *_args, **_kwargs: None,
            request_reserver=nothing_async,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: None,
            start_http_clients=nothing_async,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: events.append("migrate"),
            verify_versioned_schema=lambda: None,
            ensure_catalog_capabilities=lambda: None,
            run_database=run_database,
            start_background_tasks=dict,
            cancel_background_tasks=lambda _tasks: nothing_async(),
            cancel_shared_snapshots=nothing_async,
            shutdown_super_get_executor=lambda: None,
            shutdown_runtime_executors=lambda: None,
            close_http_clients=nothing_async,
            close_async_database=nothing_async,
            close_database=lambda: None,
        )

        async def exercise() -> None:
            async with application_lifespan(dependencies):
                pass

        asyncio.run(exercise())
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
