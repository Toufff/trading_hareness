"""Tests for the standalone intraday signal attribution backfill module."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.intraday_signal_attribution_service import (
    INTRADAY_SIGNAL_ATTRIBUTION_BACKFILL_LIMIT,
    IntradaySignalAttributionRefreshDependencies,
    refresh_intraday_signal_attributions,
)


class RefreshIntradaySignalAttributionsTests(unittest.TestCase):
    def _dependencies(self, attribution_for=None):
        return IntradaySignalAttributionRefreshDependencies(
            attribution_for=attribution_for or (lambda *_args: {"label": "new"}),
            json_safe=lambda value: value,
        )

    def test_no_rows_means_no_update_statement_runs(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = []

        changed = refresh_intraday_signal_attributions(
            connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc), dependencies=self._dependencies(),
        )

        self.assertEqual(changed, 0)
        self.assertEqual(connection.execute.call_count, 1)

    def test_unchanged_attribution_rows_are_not_rewritten(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"signal_event_id": "a", "signal_key": "k", "signal_type": "entry",
             "conditions": {}, "evidence": {"attribution": "same"}},
        ]

        changed = refresh_intraday_signal_attributions(
            connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc),
            dependencies=self._dependencies(lambda *_args: "same"),
        )

        self.assertEqual(changed, 0)
        self.assertEqual(connection.execute.call_count, 1)

    def test_changed_rows_are_written_with_one_batched_update(self):
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"signal_event_id": "a", "signal_key": "k1", "signal_type": "entry",
             "conditions": {}, "evidence": {"attribution": "old"}},
            {"signal_event_id": "b", "signal_key": "k2", "signal_type": "watch",
             "conditions": {}, "evidence": {}},
        ]

        changed = refresh_intraday_signal_attributions(
            connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc),
            dependencies=self._dependencies(lambda *_args: "new"),
        )

        self.assertEqual(changed, 2)
        self.assertEqual(connection.execute.call_count, 2)
        update_sql = connection.execute.call_args.args[0]
        self.assertIn("UPDATE quant.intraday_signal_events", update_sql)

    def test_backfill_limit_defaults_to_the_module_constant(self):
        self.assertEqual(self._dependencies().backfill_limit, INTRADAY_SIGNAL_ATTRIBUTION_BACKFILL_LIMIT)


if __name__ == "__main__":
    unittest.main()
