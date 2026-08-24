from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest

from app.ten_day_leader_ranking import rank_ten_day_candidates


class TenDayLeaderRankingTests(unittest.TestCase):
    def _rows(self, symbol: str, start: float, end: float) -> list[dict[str, object]]:
        values = [start + (end - start) * index / 10 for index in range(11)]
        first = date(2026, 8, 3)
        return [
            {
                "symbol": symbol,
                "name": symbol,
                "trading_date": first + timedelta(days=index),
                "open": value,
                "high": value,
                "low": value,
                "close": value,
                "pre_close": values[index - 1] if index else value,
                "volume": 1000 + index,
                "adj_factor": 1,
                "limit_up": value if index == 10 else value * 1.1,
                "is_suspended": False,
                "available_at": datetime(2026, 8, 13, 8, index, tzinfo=timezone.utc),
            }
            for index, value in enumerate(values)
        ]

    def test_ranks_each_price_band_board_independently(self) -> None:
        rows = [
            *self._rows("600001.SH", 10, 12),
            *self._rows("600002.SH", 10, 11),
            *self._rows("300001.SZ", 10, 14),
            *self._rows("830001.BJ", 10, 15),
        ]

        result = rank_ten_day_candidates(
            rows, date(2026, 8, 13), daily_symbols=4,
            minimum_full_market_symbols=4, per_board_limit=1,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [(item["board"], item["symbol"], item["ten_day_rank"]) for item in result["candidates"]],
            [("main", "600001.SH", 1), ("growth", "300001.SZ", 1), ("bj", "830001.BJ", 1)],
        )
        self.assertTrue(result["candidates"][0]["is_limit_up"])
        self.assertTrue(result["candidates"][0]["is_one_word_board"])

    def test_fails_closed_when_complete_adjusted_histories_miss_the_gate(self) -> None:
        rows = self._rows("600001.SH", 10, 12)
        rows[-1]["adj_factor"] = None

        result = rank_ten_day_candidates(
            rows, date(2026, 8, 13), daily_symbols=2,
            minimum_full_market_symbols=2, per_board_limit=30,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reason"], "insufficient_complete_adjusted_ten_session_histories")

    def test_fails_closed_when_same_date_cross_section_is_incomplete(self) -> None:
        result = rank_ten_day_candidates(
            self._rows("600001.SH", 10, 12), date(2026, 8, 13),
            daily_symbols=1, minimum_full_market_symbols=2, per_board_limit=30,
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "insufficient_same_date_daily_coverage")


if __name__ == "__main__":
    unittest.main()
