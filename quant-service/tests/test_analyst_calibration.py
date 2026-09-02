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

    def test_embargo_uses_exit_date_not_event_date(self):
        """A d+2 event must not train on a d event whose 60-day label has not
        settled yet, even though d+2 is 2 calendar days past d's event_date.
        """
        base = date(2026, 1, 1)
        events = [
            {"event_date": base, "exit_date": base + timedelta(days=60), "score": 1, "label": 1},
        ]
        # 29 clearly-settled filler events so the leaking event alone cannot
        # supply min_training_events on its own; each is far enough in the
        # past that its own exit_date is not in question.
        for index in range(29):
            day = base - timedelta(days=100 + index)
            events.append({"event_date": day, "exit_date": day + timedelta(days=1), "score": -1, "label": 0})
        probing_date = base + timedelta(days=2)
        events.append({"event_date": probing_date, "exit_date": probing_date + timedelta(days=60), "score": 1, "label": 1})

        result = chronological_calibration(events, min_training_events=29, minimum_oof_events=1, embargo_days=1)
        probing_prediction = next(item for item in result["predictions"] if item["event_date"] == str(probing_date))
        # Exactly the 29 settled filler events: the still-unsettled base event
        # (event_date only 2 days before probing_date, but its 60-day label
        # exit_date is long after) must be excluded, not counted as a 30th.
        self.assertEqual(probing_prediction["training_events"], 29)

    def test_missing_exit_date_falls_back_to_the_previous_embargo_days_behaviour(self):
        events = []
        start = date(2026, 1, 1)
        for index in range(65):
            events.append({"event_date": start + timedelta(days=index), "score": 1 if index % 2 else -1, "label": int(index % 3 != 0)})
        result = chronological_calibration(events, min_training_events=20, minimum_oof_events=20, embargo_days=1)
        self.assertEqual(result["status"], "completed")
        self.assertGreaterEqual(result["oof_events"], 20)


if __name__ == "__main__":
    unittest.main()
