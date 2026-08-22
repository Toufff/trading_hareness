from datetime import date, timedelta
import unittest

from app.analyst_calibration import chronological_calibration, reliability_bins


class AnalystCalibrationTests(unittest.TestCase):
    def test_insufficient_history_is_explicit(self):
        result = chronological_calibration([{"event_date": date(2026, 8, 1), "score": 1, "label": 1}])
        self.assertEqual(result["status"], "insufficient_history")
        self.assertEqual(result["live_effect"], "none")
        self.assertEqual(result["oof_events"], 0)

    def test_expanding_oof_never_uses_same_or_embargoed_day(self):
        events = []
        start = date(2026, 1, 1)
        for index in range(65):
            events.append({"event_date": start + timedelta(days=index), "score": 1 if index % 2 else -1, "label": int(index % 3 != 0)})
        result = chronological_calibration(events, min_training_events=20, minimum_oof_events=20, embargo_days=1)
        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["oof_events"], 20)
        self.assertTrue(all(item["training_events"] < 65 for item in result["predictions"]))
        self.assertIn("brier", result["model"])
        self.assertIn("log_loss", result["baseline"])

    def test_reliability_bins_ignore_empty_buckets(self):
        bins = reliability_bins([0.1, 0.9], [0, 1], bins=5)
        self.assertEqual(len(bins), 2)
        self.assertEqual(sum(item["observations"] for item in bins), 2)


if __name__ == "__main__":
    unittest.main()
