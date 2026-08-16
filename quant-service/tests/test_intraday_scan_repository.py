from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.intraday_scan_repository import first_eac_breakout_events, previous_quote_frames


class IntradayScanRepositoryTests(unittest.TestCase):
    def test_previous_quotes_are_loaded_once_per_symbol_source_pair(self) -> None:
        connection = MagicMock()
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "000001.SZ", "source_name": "tencent_free", "price": 10.0,
             "observed_at": datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)},
        ]
        frames = previous_quote_frames(
            connection,
            {"000001.SZ": "tencent_free", "600000.SH": "sina_free"},
            not_before=datetime(2026, 8, 17, 0, 59, 45, tzinfo=timezone.utc),
            observed_at=datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(frames["000001.SZ"]["price"], 10.0)
        self.assertEqual(connection.execute.call_count, 1)
        sql, params = connection.execute.call_args.args
        self.assertIn("DISTINCT ON(o.symbol,o.source_name)", sql)
        self.assertEqual(params[0], ["000001.SZ", "600000.SH"])
        self.assertEqual(params[1], ["tencent_free", "sina_free"])

    def test_first_eac_events_are_batched_and_use_earliest_event(self) -> None:
        connection = MagicMock()
        first_at = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
        connection.execute.return_value.fetchall.return_value = [
            {"symbol": "000001.SZ", "observed_at": first_at, "conditions": {"setup": "eac"}},
        ]
        events = first_eac_breakout_events(
            connection, ["000001.SZ", "000001.SZ", "600000.SH"],
            not_before=datetime(2026, 8, 17, 0, 55, tzinfo=timezone.utc),
        )
        self.assertEqual(events["000001.SZ"]["observed_at"], first_at)
        self.assertEqual(connection.execute.call_count, 1)
        sql, params = connection.execute.call_args.args
        self.assertIn("DISTINCT ON(symbol)", sql)
        self.assertIn("signal_key=symbol || ':watch:upside_breakout_eac_v3'", sql)
        self.assertEqual(params[0], ["000001.SZ", "600000.SH"])

    def test_empty_batches_do_not_query_database(self) -> None:
        connection = MagicMock()
        now = datetime(2026, 8, 17, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(previous_quote_frames(connection, {}, not_before=now, observed_at=now), {})
        self.assertEqual(first_eac_breakout_events(connection, [], not_before=now), {})
        connection.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
