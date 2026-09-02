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
            verify_strategy_contracts=mark("strategies:verify"),
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
            "db:migrate", "db:verify", "run_database", "catalog", "strategies:verify", "tasks:start", "inside",
            "tasks:cancel", "snapshot:cancel", "super_get:shutdown", "executors:shutdown",
            "http:close", "configure:off", "async_db:close", "db:close",
        ])

    def test_write_key_is_resolved_before_any_resource_is_opened(self) -> None:
        events: list[str] = []

        async def nothing_async():
            return None

        async def run_database(*_args, **_kwargs):
            return None

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: events.append("db:open"),
            open_async_database=nothing_async,
            configure_request_reserver=lambda *_args, **_kwargs: None,
            request_reserver=nothing_async,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: None,
            start_http_clients=nothing_async,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: None,
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
            resolve_write_api_key=lambda: events.append("write_key:resolved"),
        )

        async def exercise() -> None:
            async with application_lifespan(dependencies):
                pass

        asyncio.run(exercise())
        self.assertEqual(events, ["write_key:resolved", "db:open"])

    def test_startup_fails_closed_when_write_key_resolution_raises_before_any_resource_opens(self) -> None:
        events: list[str] = []

        def resolve_write_api_key() -> None:
            raise RuntimeError("QUANT_WRITE_API_KEY is required")

        async def nothing_async():
            return None

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: events.append("db:open"),
            open_async_database=nothing_async,
            configure_request_reserver=lambda *_args, **_kwargs: None,
            request_reserver=nothing_async,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: None,
            start_http_clients=nothing_async,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: None,
            verify_versioned_schema=lambda: None,
            ensure_catalog_capabilities=lambda: None,
            run_database=lambda *_args, **_kwargs: None,
            start_background_tasks=lambda: self.fail("tasks must not start"),
            cancel_background_tasks=lambda _tasks: self.fail("no tasks to cancel"),
            cancel_shared_snapshots=lambda: self.fail("no snapshot to cancel"),
            shutdown_super_get_executor=lambda: self.fail("nothing to shut down"),
            shutdown_runtime_executors=lambda: self.fail("nothing to shut down"),
            close_http_clients=lambda: self.fail("HTTP never started"),
            close_async_database=nothing_async,
            close_database=lambda: self.fail("db never opened"),
            resolve_write_api_key=resolve_write_api_key,
        )

        async def exercise() -> None:
            with self.assertRaisesRegex(RuntimeError, "QUANT_WRITE_API_KEY is required"):
                async with application_lifespan(dependencies):
                    self.fail("startup failure must not yield")

        asyncio.run(exercise())
        self.assertEqual(events, [])

    def test_control_plane_writes_disabled_skips_catalog_registration(self) -> None:
        events: list[str] = []

        async def nothing_async():
            return None

        async def run_database(operation, *, timeout_seconds: int):
            self.fail("run_database must not be called when control-plane writes are disabled")

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: None,
            open_async_database=nothing_async,
            configure_request_reserver=lambda *_args, **_kwargs: None,
            request_reserver=nothing_async,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: None,
            start_http_clients=nothing_async,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: None,
            verify_versioned_schema=lambda: events.append("verify"),
            ensure_catalog_capabilities=lambda: self.fail("catalog must not be registered"),
            run_database=run_database,
            start_background_tasks=dict,
            cancel_background_tasks=lambda _tasks: nothing_async(),
            cancel_shared_snapshots=nothing_async,
            shutdown_super_get_executor=lambda: None,
            shutdown_runtime_executors=lambda: None,
            close_http_clients=nothing_async,
            close_async_database=nothing_async,
            close_database=lambda: None,
            control_plane_writes_enabled=lambda: False,
        )

        async def exercise() -> None:
            async with application_lifespan(dependencies):
                pass

        asyncio.run(exercise())
        self.assertEqual(events, ["verify"])

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

    def test_startup_failure_releases_only_resources_already_acquired(self) -> None:
        events: list[str] = []

        async def open_async_database():
            events.append("async_db:open")

        async def start_http_clients():
            events.append("http:start")
            raise RuntimeError("local HTTP pool bootstrap failed")

        async def close_async_database():
            events.append("async_db:close")

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: events.append("db:open"),
            open_async_database=open_async_database,
            configure_request_reserver=lambda reserver, **_kwargs: events.append(
                "configure:on" if reserver is not None else "configure:off",
            ),
            request_reserver=lambda *_args, **_kwargs: None,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: events.append("metrics:init"),
            start_http_clients=start_http_clients,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: events.append("migrate"),
            verify_versioned_schema=lambda: events.append("verify"),
            ensure_catalog_capabilities=lambda: events.append("catalog"),
            run_database=lambda *_args, **_kwargs: None,
            start_background_tasks=lambda: events.append("tasks:start") or {},
            cancel_background_tasks=lambda _tasks: None,
            cancel_shared_snapshots=lambda: None,
            shutdown_super_get_executor=lambda: events.append("super_get:shutdown"),
            shutdown_runtime_executors=lambda: events.append("executors:shutdown"),
            close_http_clients=lambda: None,
            close_async_database=close_async_database,
            close_database=lambda: events.append("db:close"),
        )

        async def exercise() -> None:
            with self.assertRaisesRegex(RuntimeError, "HTTP pool bootstrap"):
                async with application_lifespan(dependencies):
                    self.fail("startup failure must not yield")

        asyncio.run(exercise())
        self.assertEqual(events, [
            "db:open", "async_db:open", "configure:on", "metrics:init", "http:start",
            "super_get:shutdown", "executors:shutdown", "configure:off", "async_db:close", "db:close",
        ])

    def test_cleanup_failure_does_not_skip_later_resources_or_mask_startup_error(self) -> None:
        events: list[str] = []

        async def open_async_database():
            events.append("async_db:open")

        async def start_http_clients():
            events.append("http:start")
            raise RuntimeError("catalog startup failed")

        async def close_async_database():
            events.append("async_db:close")
            raise RuntimeError("async close failed")

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: events.append("db:open"),
            open_async_database=open_async_database,
            configure_request_reserver=lambda reserver, **_kwargs: events.append(
                "configure:on" if reserver is not None else "configure:off",
            ),
            request_reserver=lambda *_args, **_kwargs: None,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: events.append("metrics:init"),
            start_http_clients=start_http_clients,
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: self.fail("unexpected migration"),
            verify_versioned_schema=lambda: self.fail("unexpected schema check"),
            ensure_catalog_capabilities=lambda: self.fail("unexpected catalog registration"),
            run_database=lambda *_args, **_kwargs: self.fail("unexpected database runner"),
            start_background_tasks=lambda: self.fail("tasks must not start"),
            cancel_background_tasks=lambda _tasks: self.fail("no tasks to cancel"),
            cancel_shared_snapshots=lambda: self.fail("no snapshot to cancel"),
            shutdown_super_get_executor=lambda: events.append("super_get:shutdown"),
            shutdown_runtime_executors=lambda: events.append("executors:shutdown"),
            close_http_clients=lambda: self.fail("HTTP never completed startup"),
            close_async_database=close_async_database,
            close_database=lambda: events.append("db:close"),
        )

        async def exercise() -> None:
            with self.assertRaisesRegex(RuntimeError, "catalog startup failed"):
                async with application_lifespan(dependencies):
                    self.fail("startup failure must not yield")

        asyncio.run(exercise())
        self.assertEqual(events[-5:], [
            "super_get:shutdown", "executors:shutdown", "configure:off", "async_db:close", "db:close",
        ])

    def test_executor_shutdown_runs_even_when_async_database_never_opens(self) -> None:
        """WP6: executor shutdown must not be gated on ``async_database_open``.

        The synchronous blocking executors are process-global thread pools
        created at import time; they own no async database connection. A
        failure between the synchronous and async database opens must still
        release them instead of leaking worker threads past shutdown.
        """
        events: list[str] = []

        async def open_async_database():
            events.append("async_db:open")
            raise RuntimeError("async database bootstrap failed")

        dependencies = ApplicationLifecycleDependencies(
            open_database=lambda: events.append("db:open"),
            open_async_database=open_async_database,
            configure_request_reserver=lambda *_args, **_kwargs: None,
            request_reserver=lambda *_args, **_kwargs: None,
            max_reservation_wait_seconds=1.0,
            initialize_provider_metrics=lambda: self.fail("unexpected metrics init"),
            start_http_clients=lambda: self.fail("unexpected HTTP start"),
            legacy_schema_bootstrap_enabled=lambda: False,
            migrate_database=lambda: self.fail("unexpected migration"),
            verify_versioned_schema=lambda: self.fail("unexpected schema check"),
            ensure_catalog_capabilities=lambda: self.fail("unexpected catalog registration"),
            run_database=lambda *_args, **_kwargs: self.fail("unexpected database runner"),
            start_background_tasks=lambda: self.fail("tasks must not start"),
            cancel_background_tasks=lambda _tasks: self.fail("no tasks to cancel"),
            cancel_shared_snapshots=lambda: self.fail("no snapshot to cancel"),
            shutdown_super_get_executor=lambda: events.append("super_get:shutdown"),
            shutdown_runtime_executors=lambda: events.append("executors:shutdown"),
            close_http_clients=lambda: self.fail("HTTP never started"),
            close_async_database=lambda: self.fail("async database never finished opening"),
            close_database=lambda: events.append("db:close"),
        )

        async def exercise() -> None:
            with self.assertRaisesRegex(RuntimeError, "async database bootstrap failed"):
                async with application_lifespan(dependencies):
                    self.fail("startup failure must not yield")

        asyncio.run(exercise())
        self.assertEqual(events, [
            "db:open", "async_db:open", "super_get:shutdown", "executors:shutdown", "db:close",
        ])

    def test_lifespan_configures_structured_logging_before_any_resource_opens(self) -> None:
        from app import logging_config

        original = logging_config._configured
        try:
            logging_config._configured = False
            events: list[str] = []

            async def nothing_async():
                return None

            dependencies = ApplicationLifecycleDependencies(
                open_database=lambda: events.append("db:open"),
                open_async_database=nothing_async,
                configure_request_reserver=lambda *_args, **_kwargs: None,
                request_reserver=nothing_async,
                max_reservation_wait_seconds=1.0,
                initialize_provider_metrics=lambda: None,
                start_http_clients=nothing_async,
                legacy_schema_bootstrap_enabled=lambda: False,
                migrate_database=lambda: None,
                verify_versioned_schema=lambda: None,
                ensure_catalog_capabilities=lambda: None,
                run_database=lambda *_args, **_kwargs: nothing_async(),
                start_background_tasks=lambda: {},
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

            self.assertFalse(logging_config._configured)
            asyncio.run(exercise())
            self.assertTrue(logging_config._configured)
        finally:
            logging_config._configured = original


if __name__ == "__main__":
    unittest.main()
