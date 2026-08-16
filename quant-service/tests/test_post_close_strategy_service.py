from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.post_close_strategy_service import candidates, retry_window, run


class PostCloseStrategyServiceTests(unittest.TestCase):
    def _database(self) -> tuple[MagicMock, MagicMock]:
        database = MagicMock()
        connection = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        return database, connection

    def test_candidate_loader_uses_only_persisted_rows_and_exact_context(self) -> None:
        database, connection = self._database()
        coverage_result = MagicMock()
        coverage_result.fetchone.return_value = {"symbols": 2}
        rows_result = MagicMock()
        rows_result.fetchall.return_value = [{"symbol": "000001.SZ", "close": 10}]
        connection.execute.side_effect = [coverage_result, rows_result]
        exact_context = {"000001.SZ": {"sector_key": "885001.TI"}}
        screen = MagicMock(return_value={"status": "completed", "candidates": []})
        as_of_date = date(2026, 8, 14)

        payload = candidates(
            database, as_of_date, 20, 2, board_context=lambda value: exact_context,
            screen=screen, daily_base_structure=lambda values: {},
            forming_structure=lambda values: {}, fresh_start_structure=lambda values: {},
        )

        self.assertEqual(payload["status"], "completed")
        args, kwargs = screen.call_args
        self.assertEqual(args[:4], (as_of_date, 20, 2, 2))
        self.assertEqual(args[4], [{"symbol": "000001.SZ", "close": 10}])
        self.assertEqual(args[5], exact_context)
        self.assertIn("daily_base_structure", kwargs)

    def test_run_preserves_explicit_date_and_persists_candidate_evidence(self) -> None:
        database, connection = self._database()
        latest_result = MagicMock()
        latest_result.fetchone.return_value = {"trading_date": date(2026, 8, 13)}
        inserted_result = MagicMock()
        inserted_result.fetchone.return_value = {"run_id": "c7a3668d-02fc-4d50-8f97-923f7f0f430d"}
        connection.execute.side_effect = [latest_result, inserted_result, MagicMock(), MagicMock()]
        target_date = date(2026, 8, 14)
        loaded = {
            "status": "completed", "as_of_date": str(target_date),
            "candidates": [{
                "symbol": "000001.SZ", "candidate_type": "base_ready_30d", "score": 88.0,
                "structure": {"status": "ready"}, "board_context": {"exact_member_mapping": True},
                "risk_flags": [],
            }],
            "source_status": {"daily_bars": 30, "daily_symbols": 5000, "exact_board_context_symbols": 1},
            "summary": {"returned": 1},
        }
        loader = MagicMock(return_value=loaded)
        request = SimpleNamespace(as_of_date=target_date, limit=20, minimum_full_market_symbols=5000)

        payload = run(
            database, request, model_version="post-close-test-v1", candidate_loader=loader, json_safe=lambda value: value,
        )

        loader.assert_called_once_with(target_date, 20, 5000)
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["as_of_date"], str(target_date))
        self.assertEqual(payload["model_version"], "post-close-test-v1")
        self.assertEqual(connection.execute.call_count, 4)

    def test_retry_window_is_shanghai_clock_bounded(self) -> None:
        china = ZoneInfo("Asia/Shanghai")
        self.assertFalse(retry_window(datetime(2026, 8, 14, 18, 54, tzinfo=china)))
        self.assertTrue(retry_window(datetime(2026, 8, 14, 18, 55, tzinfo=china)))
        self.assertTrue(retry_window(datetime(2026, 8, 14, 20, 29, 59, tzinfo=china)))
        self.assertFalse(retry_window(datetime(2026, 8, 14, 20, 30, tzinfo=china)))


if __name__ == "__main__":
    unittest.main()
