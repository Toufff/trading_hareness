from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.intraday_event_retention import ephemeral_signal_retention_days, prune_ephemeral_signal_events


class EphemeralSignalRetentionTests(unittest.TestCase):
    def test_retention_window_is_conservative_and_bounded(self) -> None:
        self.assertEqual(ephemeral_signal_retention_days({}), 90)
        self.assertEqual(ephemeral_signal_retention_days({"INTRADAY_EPHEMERAL_SIGNAL_RETENTION_DAYS": "10"}), 60)
        self.assertEqual(ephemeral_signal_retention_days({"INTRADAY_EPHEMERAL_SIGNAL_RETENTION_DAYS": "999"}), 180)
        self.assertEqual(ephemeral_signal_retention_days({"INTRADAY_EPHEMERAL_SIGNAL_RETENTION_DAYS": "bad"}), 90)

    def test_prune_query_only_targets_ephemeral_states_without_delivery_or_outcome(self) -> None:
        class Result:
            def fetchall(self):
                return [{"state": "suppressed"}, {"state": "confirming"}, {"state": "suppressed"}]

        class Connection:
            def __init__(self) -> None:
                self.sql = ""
                self.params = None

            def execute(self, sql, params):
                self.sql, self.params = sql, params
                return Result()

        connection = Connection()
        cutoff = datetime(2026, 8, 16, tzinfo=timezone.utc)
        result = prune_ephemeral_signal_events(connection, cutoff=cutoff)
        self.assertEqual(result, {"suppressed": 2, "confirming": 1, "detected": 0, "invalidated": 0, "total": 3})
        self.assertEqual(connection.params, (cutoff,))
        self.assertIn("event.state IN ('suppressed','confirming','detected','invalidated')", connection.sql)
        self.assertIn("quant.intraday_signal_outcomes", connection.sql)
        self.assertIn("quant.intraday_alert_deliveries", connection.sql)
        self.assertNotIn("'confirmed'", connection.sql)
        self.assertNotIn("'alerted'", connection.sql)
