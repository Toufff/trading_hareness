from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

from app.universe_history import point_in_time_membership_predicate, sync_universe_membership_history


class UniverseHistoryTests(unittest.TestCase):
    def test_point_in_time_predicate_accepts_repository_bar_alias(self):
        self.assertEqual(
            point_in_time_membership_predicate("membership", "b"),
            "membership.effective_from<=b.trading_date AND "
            "(membership.effective_to IS NULL OR membership.effective_to>=b.trading_date)",
        )

    def test_sync_closes_missing_and_opens_new_members(self):
        connection = MagicMock()
        connection.execute.side_effect = [MagicMock(rowcount=1), MagicMock(rowcount=2), MagicMock(rowcount=3)]
        result = sync_universe_membership_history(
            connection, "all_a", date(2026, 8, 17), ["600000.SH", "000001.SZ"], source="test",
        )
        self.assertEqual(result, {"opened": 3, "closed": 2, "discarded_same_day": 1, "active": 2})
        close_sql, close_params = connection.execute.call_args_list[1].args
        self.assertIn("effective_to=%s", close_sql)
        self.assertEqual(close_params[0], date(2026, 8, 16))
        open_sql = connection.execute.call_args_list[2].args[0]
        self.assertIn("effective_to IS NULL", open_sql)

    def test_empty_snapshot_only_closes_existing_intervals(self):
        connection = MagicMock()
        connection.execute.side_effect = [MagicMock(rowcount=1), MagicMock(rowcount=4)]
        result = sync_universe_membership_history(
            connection, "all_a", date(2026, 8, 17), [], source="test",
        )
        self.assertEqual(result, {"opened": 0, "closed": 4, "discarded_same_day": 1, "active": 0})
        self.assertEqual(connection.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
