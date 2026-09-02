"""WP6: the split quant-db-fast / quant-db-batch executor lanes.

Audit (section B, HIGH): a single ``quant-db`` pool made a 300s post-close
stage share capacity with a 10s health/intraday read; the fix splits the pool
into two bounded lanes and picks one automatically from the timeout budget so
every existing ``run_database_blocking(..., timeout_seconds=...)`` call site
benefits without being edited individually.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app.runtime_executors import (
    DB_FAST_LANE_MAX_TIMEOUT_SECONDS,
    resolve_database_lane,
    run_database_blocking,
    runtime_executor_status,
)


class ResolveDatabaseLaneTests(unittest.TestCase):
    def test_short_timeout_defaults_to_fast(self) -> None:
        self.assertEqual(resolve_database_lane(10, None), "fast")

    def test_default_timeout_is_fast(self) -> None:
        self.assertEqual(resolve_database_lane(DB_FAST_LANE_MAX_TIMEOUT_SECONDS, None), "fast")

    def test_long_timeout_defaults_to_batch(self) -> None:
        self.assertEqual(resolve_database_lane(DB_FAST_LANE_MAX_TIMEOUT_SECONDS + 0.01, None), "batch")
        self.assertEqual(resolve_database_lane(300, None), "batch")

    def test_explicit_lane_always_wins(self) -> None:
        self.assertEqual(resolve_database_lane(300, "fast"), "fast")
        self.assertEqual(resolve_database_lane(5, "batch"), "batch")

    def test_unknown_explicit_lane_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_database_lane(5, "medium")


class RunDatabaseBlockingLaneRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_call_runs_on_the_fast_thread_pool(self) -> None:
        thread_names: list[str] = []

        def record() -> str:
            name = threading.current_thread().name
            thread_names.append(name)
            return name

        result = await run_database_blocking(record, timeout_seconds=5)
        self.assertTrue(result.startswith("quant-db-fast"), result)
        self.assertEqual(thread_names, [result])

    async def test_long_call_runs_on_the_batch_thread_pool(self) -> None:
        def record() -> str:
            return threading.current_thread().name

        result = await run_database_blocking(record, timeout_seconds=120)
        self.assertTrue(result.startswith("quant-db-batch"), result)

    async def test_explicit_lane_overrides_timeout_inference(self) -> None:
        def record() -> str:
            return threading.current_thread().name

        result = await run_database_blocking(record, timeout_seconds=5, lane="batch")
        self.assertTrue(result.startswith("quant-db-batch"), result)


class RuntimeExecutorStatusTests(unittest.TestCase):
    def test_status_exposes_both_lane_watermarks_and_a_compat_alias(self) -> None:
        status = runtime_executor_status()
        self.assertIn("public_source", status)
        self.assertIn("database_fast", status)
        self.assertIn("database_batch", status)
        # Backward-compatible alias for existing health-payload consumers.
        self.assertEqual(status["database"], status["database_fast"])
        for key in ("database_fast", "database_batch"):
            self.assertIn("workers", status[key])
            self.assertIn("occupied", status[key])


class ShutdownRuntimeExecutorsWaitTests(unittest.TestCase):
    """Exercise the wait-loop logic without touching the real global pools.

    The real ``ThreadPoolExecutor``/``BlockingExecutorBoundary`` singletons are
    shared with the rest of the test process; shutting them down for real
    here would break every later test that submits work through
    ``run_database_blocking``. Only their ``.shutdown``/``.status`` methods
    are patched.
    """

    def test_waits_for_in_flight_work_then_returns_once_idle(self) -> None:
        import app.runtime_executors as rex

        statuses = iter([{"occupied": 1}, {"occupied": 1}, {"occupied": 0}])

        with patch.object(rex.public_source_executor, "shutdown") as public_shutdown, \
             patch.object(rex.database_fast_executor, "shutdown") as fast_shutdown, \
             patch.object(rex.database_batch_executor, "shutdown") as batch_shutdown, \
             patch.object(rex.public_source_boundary, "status", return_value={"occupied": 0}), \
             patch.object(rex.database_batch_boundary, "status", return_value={"occupied": 0}), \
             patch.object(rex.database_fast_boundary, "status", side_effect=lambda: next(statuses)), \
             patch("app.runtime_executors.time.sleep") as sleep_mock:
            rex.shutdown_runtime_executors(wait_for_inflight_seconds=5.0)

        public_shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        fast_shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        batch_shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        self.assertGreaterEqual(sleep_mock.call_count, 2)

    def test_zero_wait_shuts_down_without_polling_status(self) -> None:
        import app.runtime_executors as rex

        with patch.object(rex.public_source_executor, "shutdown"), \
             patch.object(rex.database_fast_executor, "shutdown"), \
             patch.object(rex.database_batch_executor, "shutdown"), \
             patch.object(rex.database_fast_boundary, "status") as status_mock, \
             patch("app.runtime_executors.time.sleep") as sleep_mock:
            rex.shutdown_runtime_executors(wait_for_inflight_seconds=0)

        status_mock.assert_not_called()
        sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
