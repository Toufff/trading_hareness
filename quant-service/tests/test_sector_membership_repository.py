"""Pure and SQL-boundary checks for sector membership provenance."""

from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.sector_membership_repository import (
    OBSERVED_SNAPSHOT,
    PROVIDER_INTERVAL,
    membership_interval,
    point_in_time_membership_predicate,
)


class SectorMembershipRepositoryTests(unittest.TestCase):
    def test_missing_provider_start_becomes_observed_snapshot_not_1900(self) -> None:
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        start, end, start_basis, end_basis = membership_interval(
            {"con_code": "000001.SZ", "in_date": None, "out_date": None}, observed_at,
            parse_date=lambda value: date.fromisoformat(value) if value else None,
        )
        self.assertEqual(start, date(2026, 8, 31))
        self.assertIsNone(end)
        self.assertEqual((start_basis, end_basis), (OBSERVED_SNAPSHOT, OBSERVED_SNAPSHOT))

    def test_provider_interval_keeps_source_dates(self) -> None:
        observed_at = datetime(2026, 8, 31, 1, tzinfo=timezone.utc)
        start, end, start_basis, end_basis = membership_interval(
            {"in_date": "2020-01-02", "out_date": "2025-03-04"}, observed_at,
            parse_date=lambda value: date.fromisoformat(value) if value else None,
        )
        self.assertEqual((start, end), (date(2020, 1, 2), date(2025, 3, 4)))
        self.assertEqual((start_basis, end_basis), (PROVIDER_INTERVAL, PROVIDER_INTERVAL))

    def test_strict_predicate_excludes_legacy_and_bounds_known_time(self) -> None:
        sql = point_in_time_membership_predicate("member")
        self.assertIn("effective_from_basis IN ('provider_interval','observed_snapshot')", sql)
        self.assertIn("member.known_at <=", sql)
        self.assertIn("17:00:00", sql)
        self.assertNotIn("1900", sql)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
