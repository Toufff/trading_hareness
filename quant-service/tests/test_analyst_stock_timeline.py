from datetime import datetime, timezone
import unittest

from app.analyst_stock_timeline import _nearest_bar


class AnalystStockTimelineTests(unittest.TestCase):
    def test_nearest_bar_maps_second_precision_event(self):
        bars = [{"bar_time": datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc), "close": 98.6}]
        result = _nearest_bar(datetime(2026, 8, 17, 6, 0, 7, tzinfo=timezone.utc), bars)
        self.assertEqual(result["mapping_status"], "mapped")
        self.assertEqual(result["offset_seconds"], -7)

    def test_nearest_bar_fails_closed_outside_window(self):
        bars = [{"bar_time": datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc), "close": 98.6}]
        result = _nearest_bar(datetime(2026, 8, 17, 6, 4, tzinfo=timezone.utc), bars)
        self.assertEqual(result["mapping_status"], "outside_three_minute_window")


if __name__ == "__main__":
    unittest.main()
