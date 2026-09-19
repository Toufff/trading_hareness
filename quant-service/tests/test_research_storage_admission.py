from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.research_storage_admission import ResearchStorageAdmission, governance
from app.runtime_resources import (
    MANAGED_DIRECTORY_CACHE_SECONDS,
    ManagedDirectoryCache,
    research_storage_governance,
)


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


class ResearchStorageAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_capture_decision_is_cached_and_never_changes_core_default(self) -> None:
        status = {"allow_nonessential_high_frequency": False, "state": "stop"}
        run_database = AsyncMock(return_value=status)
        admission = ResearchStorageAdmission(lambda: status, run_database, cache_seconds=60)

        first = await admission.optional_high_frequency_allowed()
        second = await admission.optional_high_frequency_allowed()

        self.assertEqual(first, (False, status))
        self.assertEqual(second, (False, status))
        self.assertEqual(run_database.await_count, 1)
        self.assertEqual(run_database.await_args.kwargs["timeout_seconds"], 10)

    async def test_governance_uses_managed_database_and_artifact_budgets(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {"bytes": 100}
        database = MagicMock()
        database.transaction.return_value = _Transaction(connection)

        result = governance(
            database,
            environ={
                "QUANT_DATA_DIR": "/tmp/quant-test", "QUANT_RESEARCH_STORAGE_SOFT_BYTES": "1000",
                "QUANT_HOT_DATABASE_SOFT_BYTES": "800", "QUANT_RESEARCH_STORAGE_WARNING_RATIO": "0.8",
                "QUANT_RESEARCH_STORAGE_STOP_RATIO": "0.9",
            },
            directory_bytes=lambda path: 200 if path.as_posix() == "/tmp/quant-test" else 0,
        )

        self.assertEqual(result["managed"]["used_bytes"], 300)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(result["allow_nonessential_high_frequency"])


class _FakeClock:
    """A monotonic clock a test can move by hand."""

    def __init__(self) -> None:
        self.seconds = 1_000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


class ManagedDirectoryCacheTests(unittest.TestCase):
    """/health must not answer a liveness check with a filesystem inventory.

    ``managed_directory_bytes`` walks every file below the research directory,
    and /health asked for it on every request: 4.3-4.9 s on this host's HDD,
    long enough to break a release's health probes.  The cache bounds how often
    that walk happens without changing what the number means.
    """

    def setUp(self) -> None:
        self.clock = _FakeClock()
        self.walks: list[Path] = []
        self.size = 4_096

        def walk(path: Path) -> int:
            self.walks.append(path)
            return self.size

        self.cache = ManagedDirectoryCache(
            ttl_seconds=60.0, walk=walk, monotonic=self.clock,
            wall_clock=lambda: datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc))

    def test_the_first_call_walks_and_the_next_minute_does_not(self) -> None:
        first = self.cache.measure(Path("/var/lib/quant"))
        self.assertEqual((first.used_bytes, first.cached, first.age_seconds), (4_096, False, 0.0))
        self.clock.advance(59.0)
        second = self.cache.measure(Path("/var/lib/quant"))
        self.assertEqual((second.used_bytes, second.cached, second.age_seconds), (4_096, True, 59.0))
        self.assertEqual(len(self.walks), 1)

    def test_the_walk_happens_again_once_the_ttl_has_passed(self) -> None:
        self.cache.measure(Path("/var/lib/quant"))
        self.clock.advance(60.0)
        self.size = 8_192
        refreshed = self.cache.measure(Path("/var/lib/quant"))
        self.assertEqual((refreshed.used_bytes, refreshed.cached, refreshed.age_seconds),
                         (8_192, False, 0.0))
        self.assertEqual(len(self.walks), 2)

    def test_a_caller_that_needs_the_current_size_can_force_a_walk(self) -> None:
        # The size-on-demand path: an admission decision that must be made
        # against the current size, not the recent one.
        self.cache.measure(Path("/var/lib/quant"))
        self.clock.advance(1.0)
        self.size = 1_024
        forced = self.cache.measure(Path("/var/lib/quant"), force=True)
        self.assertEqual((forced.used_bytes, forced.cached), (1_024, False))
        self.assertEqual(len(self.walks), 2)
        # ... and the forced walk becomes the cached answer, so the next
        # caller inside the TTL does not walk again either.
        self.assertTrue(self.cache.measure(Path("/var/lib/quant")).cached)
        self.assertEqual(len(self.walks), 2)

    def test_the_number_itself_is_never_transformed(self) -> None:
        self.size = 123_456_789
        self.assertEqual(self.cache.measure(Path("/var/lib/quant")).used_bytes, 123_456_789)

    def test_two_directories_are_measured_independently(self) -> None:
        self.cache.measure(Path("/var/lib/quant"))
        self.size = 99
        other = self.cache.measure(Path("/var/lib/other"))
        self.assertEqual((other.used_bytes, other.cached), (99, False))
        self.assertEqual(self.cache.measure(Path("/var/lib/quant")).used_bytes, 4_096)
        self.assertEqual(len(self.walks), 2)

    def test_invalidate_forces_the_next_call_to_walk(self) -> None:
        self.cache.measure(Path("/var/lib/quant"))
        self.cache.invalidate(Path("/var/lib/quant"))
        self.assertFalse(self.cache.measure(Path("/var/lib/quant")).cached)
        self.assertEqual(len(self.walks), 2)

    def test_the_default_ttl_is_the_documented_minute(self) -> None:
        self.assertEqual(MANAGED_DIRECTORY_CACHE_SECONDS, 60.0)

    def test_the_health_payload_publishes_how_old_the_measurement_is(self) -> None:
        self.cache.measure(Path("/var/lib/quant"))
        self.clock.advance(12.5)
        measurement = self.cache.measure(Path("/var/lib/quant"))
        payload = research_storage_governance(
            hot_database_bytes=100, artifact_bytes=measurement.used_bytes,
            research_budget_bytes=1_000_000, hot_database_budget_bytes=800_000,
            warning_ratio=0.8, stop_ratio=0.9, artifact_measurement=measurement)
        artifacts = payload["artifacts"]
        # The byte count keeps its exact previous meaning; only its age is new.
        self.assertEqual(artifacts["used_bytes"], 4_096)
        self.assertEqual(artifacts["measured_at"], "2026-09-19T12:00:00+00:00")
        self.assertEqual(artifacts["age_seconds"], 12.5)
        self.assertTrue(artifacts["cached"])
        self.assertEqual(artifacts["ttl_seconds"], 60.0)

    def test_a_payload_without_a_measurement_is_shaped_exactly_as_before(self) -> None:
        payload = research_storage_governance(
            hot_database_bytes=100, artifact_bytes=200, research_budget_bytes=1_000,
            hot_database_budget_bytes=800, warning_ratio=0.8, stop_ratio=0.9)
        self.assertEqual(payload["artifacts"], {"used_bytes": 200})
