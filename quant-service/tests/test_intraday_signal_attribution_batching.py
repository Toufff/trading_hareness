"""WP6 / wp10-report.md: refresh_intraday_signal_attributions used to issue
one UPDATE per changed row. It now issues exactly one batched
``UPDATE ... FROM unnest(...)`` regardless of how many rows changed.

Split out of test_platform_boundaries.py (which is bounded by
scripts/verify_architecture.py's oversized-test-module guard) rather than
folded in there.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import app.main as main_module


class IntradaySignalAttributionBatchingTests(unittest.TestCase):
    def test_batches_changed_rows_into_one_update(self) -> None:
        signal_id_a, signal_id_b = uuid.uuid4(), uuid.uuid4()
        rows = [
            {"signal_event_id": signal_id_a, "signal_key": "k1", "signal_type": "entry",
             "conditions": {}, "evidence": {"attribution": "old"}},
            {"signal_event_id": signal_id_b, "signal_key": "k2", "signal_type": "entry",
             "conditions": {}, "evidence": {"attribution": "old"}},
        ]
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = rows

        with patch("app.main.intraday_signal_attribution", return_value={"label": "new"}):
            changed = main_module.refresh_intraday_signal_attributions(
                connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc),
            )

        self.assertEqual(changed, 2)
        # Exactly one SELECT and one batched UPDATE, never one UPDATE per row.
        self.assertEqual(connection.execute.call_count, 2)
        update_query, update_params = connection.execute.call_args_list[1].args
        self.assertIn("UPDATE quant.intraday_signal_events AS t", update_query)
        self.assertIn("FROM unnest(%s::uuid[],%s::text[])", update_query)
        ids, evidence_json = update_params
        self.assertEqual(ids, [signal_id_a, signal_id_b])
        self.assertEqual(len(evidence_json), 2)
        self.assertIn('"label": "new"', evidence_json[0])

    def test_unchanged_rows_never_trigger_the_update(self) -> None:
        rows = [
            {"signal_event_id": uuid.uuid4(), "signal_key": "k1", "signal_type": "entry",
             "conditions": {}, "evidence": {"attribution": {"label": "same"}}},
        ]
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = rows

        with patch("app.main.intraday_signal_attribution", return_value={"label": "same"}):
            changed = main_module.refresh_intraday_signal_attributions(
                connection, cutoff=datetime(2026, 8, 13, tzinfo=timezone.utc),
            )

        self.assertEqual(changed, 0)
        self.assertEqual(connection.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
