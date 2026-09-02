"""WP6 / wp10-report.md "需要他人处理" #4: persist_tushare_rows's raw-evidence
write used to be one INSERT per row. It is now one batched
``INSERT ... SELECT FROM unnest(...)``, deduplicated client-side so a single
statement never targets the same conflict key twice.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import app.main as main


class PersistTushareRowsBatchingTests(unittest.TestCase):
    def test_empty_rows_skips_the_raw_insert_entirely(self) -> None:
        connection = MagicMock()
        with patch("app.main.normalize_tushare_rows", return_value=0) as normalize:
            result = main.persist_tushare_rows(
                connection, "daily", "req-1", [], "tushare_primary", datetime(2026, 8, 21, tzinfo=timezone.utc),
            )
        self.assertEqual(result, 0)
        connection.execute.assert_not_called()
        normalize.assert_called_once()

    def test_multiple_rows_produce_exactly_one_insert_statement(self) -> None:
        rows = [
            {"ts_code": "600000.SH", "trade_date": "20260821"},
            {"ts_code": "600001.SH", "trade_date": "20260821"},
            {"ts_code": "600002.SH", "trade_date": "20260821"},
        ]
        connection = MagicMock()
        available_at = datetime(2026, 8, 21, tzinfo=timezone.utc)

        with patch("app.main.normalize_tushare_rows", return_value=3):
            result = main.persist_tushare_rows(connection, "daily", "req-1", rows, "tushare_primary", available_at)

        self.assertEqual(result, 3)
        connection.execute.assert_called_once()
        query, params = connection.execute.call_args.args
        self.assertIn("INSERT INTO quant.tushare_raw_records", query)
        self.assertIn("FROM unnest(%s::integer[],%s::text[],%s::text[],%s::text[])", query)
        self.assertIn("ON CONFLICT(provider_key,api_name,record_key,content_sha256)", query)
        provider_key, api_name, request_key, returned_available_at, record_indexes, record_keys, content_hashes, row_jsons = params
        self.assertEqual(provider_key, "tushare_primary")
        self.assertEqual(api_name, "daily")
        self.assertEqual(request_key, "req-1")
        self.assertEqual(returned_available_at, available_at)
        self.assertEqual(len(record_indexes), 3)
        self.assertEqual(len(record_keys), 3)
        self.assertEqual(len(content_hashes), 3)
        self.assertEqual(len(row_jsons), 3)
        self.assertEqual(json.loads(row_jsons[0])["ts_code"], "600000.SH")

    def test_duplicate_conflict_keys_within_one_batch_are_deduplicated_last_wins(self) -> None:
        """A single INSERT cannot target the same conflict key twice."""
        rows = [
            {"ts_code": "600000.SH", "trade_date": "20260821", "close": 10.0},
            {"ts_code": "600000.SH", "trade_date": "20260821", "close": 10.0},  # exact duplicate
        ]
        connection = MagicMock()

        with patch("app.main.normalize_tushare_rows", return_value=1):
            main.persist_tushare_rows(
                connection, "daily", "req-1", rows, "tushare_primary", datetime(2026, 8, 21, tzinfo=timezone.utc),
            )

        _query, params = connection.execute.call_args.args
        record_keys, content_hashes = params[5], params[6]
        # Only one distinct (record_key, content_sha256) pair remains.
        self.assertEqual(len(set(zip(record_keys, content_hashes))), 1)
        self.assertEqual(len(record_keys), 1)

    def test_still_normalizes_all_original_rows_including_deduplicated_ones(self) -> None:
        """Dedup only affects the raw-evidence write, not canonical normalization."""
        rows = [
            {"ts_code": "600000.SH", "trade_date": "20260821"},
            {"ts_code": "600000.SH", "trade_date": "20260821"},
        ]
        connection = MagicMock()

        with patch("app.main.normalize_tushare_rows", return_value=2) as normalize:
            main.persist_tushare_rows(
                connection, "daily", "req-1", rows, "tushare_primary", datetime(2026, 8, 21, tzinfo=timezone.utc),
            )

        self.assertEqual(normalize.call_args.args[2], rows)


if __name__ == "__main__":
    unittest.main()
