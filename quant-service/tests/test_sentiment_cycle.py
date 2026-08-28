"""Coverage for the five short-term sentiment readings.

Each test pins a distinction the existing regime could not make. The regime
carries breadth, index volume and a sealed count; none of those separate a
tape where boards hold from one where half of them break, and this session's
own 18,823-pair study showed those are different markets.
"""

from __future__ import annotations

from datetime import date
import unittest

from app.sentiment_cycle import (
    DEFAULT_STAGE,
    board_heights,
    broken_symbols,
    classify_stage,
    executable_premium_pct,
    sealed_symbols,
    sentiment_reading,
)


def _bar(symbol, *, close, limit_up=11.0, high=None, open_=10.0):
    return {"symbol": symbol, "close": close, "limit_up": limit_up,
            "high": close if high is None else high, "open": open_}


class SealedAndBrokenTests(unittest.TestCase):
    def test_a_close_at_the_limit_is_sealed(self):
        self.assertEqual(sealed_symbols([_bar("A", close=11.0)]), {"A"})

    def test_a_touch_that_did_not_hold_is_broken_not_sealed(self):
        rows = [_bar("A", close=10.4, high=11.0)]
        self.assertEqual(sealed_symbols(rows), set())
        self.assertEqual(broken_symbols(rows), {"A"})

    def test_a_name_that_never_reached_the_limit_is_neither(self):
        rows = [_bar("A", close=10.2, high=10.3)]
        self.assertEqual(sealed_symbols(rows), set())
        self.assertEqual(broken_symbols(rows), set())

    def test_a_missing_limit_price_is_excluded_rather_than_assumed(self):
        rows = [{"symbol": "A", "close": 11.0, "limit_up": None, "high": 11.0}]
        self.assertEqual(sealed_symbols(rows), set())
        self.assertEqual(broken_symbols(rows), set())


class BoardHeightTests(unittest.TestCase):
    def _sessions(self, pattern):
        # pattern: list of iterables of sealed symbols per session, oldest first
        return [(date(2026, 8, 20 + index), [_bar(symbol, close=11.0) for symbol in sealed])
                for index, sealed in enumerate(pattern)]

    def test_a_run_is_counted_back_from_the_last_session(self):
        heights = board_heights(self._sessions([{"A"}, {"A"}, {"A"}]))
        self.assertEqual(heights["A"], 3)

    def test_a_gap_ends_the_run(self):
        heights = board_heights(self._sessions([{"A"}, set(), {"A"}]))
        self.assertEqual(heights["A"], 1)

    def test_a_session_the_name_did_not_trade_breaks_the_run(self):
        # A suspension must not read as a board that held through it.
        sessions = [
            (date(2026, 8, 20), [_bar("A", close=11.0)]),
            (date(2026, 8, 21), []),
            (date(2026, 8, 24), [_bar("A", close=11.0)]),
        ]
        self.assertEqual(board_heights(sessions)["A"], 1)

    def test_only_names_sealed_on_the_last_session_have_a_height(self):
        heights = board_heights(self._sessions([{"A", "B"}, {"A"}]))
        self.assertEqual(set(heights), {"A"})


class ExecutablePremiumTests(unittest.TestCase):
    def test_the_premium_is_open_to_close_not_close_to_close(self):
        # A name locked at yesterday's close could not be bought there, so the
        # close-to-close figure most write-ups quote is not takeable.
        rows = [_bar("A", close=12.0, open_=11.5, limit_up=13.0)]
        self.assertAlmostEqual(executable_premium_pct(rows, {"A"}),
                               (12.0 / 11.5 - 1) * 100, places=6)

    def test_names_outside_the_prior_seal_set_are_ignored(self):
        rows = [_bar("A", close=12.0, open_=11.5), _bar("B", close=20.0, open_=10.0)]
        self.assertAlmostEqual(executable_premium_pct(rows, {"A"}),
                               (12.0 / 11.5 - 1) * 100, places=6)

    def test_no_surviving_names_reports_none_rather_than_zero(self):
        self.assertIsNone(executable_premium_pct([], {"A"}))


class SentimentReadingTests(unittest.TestCase):
    def test_the_broken_rate_is_over_attempts_not_over_the_market(self):
        sessions = [(date(2026, 8, 28), [
            _bar("A", close=11.0), _bar("B", close=11.0),
            _bar("C", close=10.4, high=11.0),
            _bar("D", close=10.1, high=10.2),
        ])]
        reading = sentiment_reading(sessions)
        self.assertEqual(reading["sealed_count"], 2)
        self.assertEqual(reading["broken_count"], 1)
        self.assertAlmostEqual(reading["broken_rate"], 1 / 3, places=6)

    def test_promotion_is_measured_against_yesterdays_sealed_names(self):
        sessions = [
            (date(2026, 8, 27), [_bar("A", close=11.0), _bar("B", close=11.0)]),
            (date(2026, 8, 28), [_bar("A", close=11.0), _bar("B", close=10.0, high=10.1)]),
        ]
        self.assertAlmostEqual(sentiment_reading(sessions)["promotion_rate"], 0.5, places=6)

    def test_a_first_session_has_no_promotion_rate_rather_than_zero(self):
        reading = sentiment_reading([(date(2026, 8, 28), [_bar("A", close=11.0)])])
        self.assertIsNone(reading["promotion_rate"],
                          "an unknown rate must not read as a frozen tape")

    def test_a_day_with_no_attempts_reports_no_broken_rate(self):
        reading = sentiment_reading([(date(2026, 8, 28), [_bar("A", close=10.1, high=10.2)])])
        self.assertIsNone(reading["broken_rate"])

    def test_empty_input_does_not_raise(self):
        self.assertEqual(sentiment_reading([])["stage"], DEFAULT_STAGE)


class PremiumSurvivorshipTests(unittest.TestCase):
    """The premium must include yesterday's boards that fell, not only those that held.

    This is the bias the loader is shaped to avoid: averaging only the names
    that limited up twice reports a premium several times the real one, and it
    is exactly the shape of the close-to-close figure most write-ups quote.
    """

    def test_a_name_that_fell_still_counts_against_the_premium(self):
        sessions = [
            (date(2026, 8, 27), [_bar("A", close=11.0), _bar("B", close=11.0)]),
            (date(2026, 8, 28), [
                _bar("A", close=12.1, open_=11.0, limit_up=12.1),   # held, +10%
                _bar("B", close=9.9, open_=11.0, high=11.0, limit_up=12.1),  # fell, -10%
            ]),
        ]
        premium = sentiment_reading(sessions)["prior_limit_up_premium_pct"]
        self.assertLess(premium, 1.0,
                        "dropping the fallen name would report roughly +10% here")

    def test_the_survivor_only_view_is_the_number_being_avoided(self):
        survivors_only = [
            (date(2026, 8, 27), [_bar("A", close=11.0), _bar("B", close=11.0)]),
            (date(2026, 8, 28), [_bar("A", close=12.1, open_=11.0, limit_up=12.1)]),
        ]
        self.assertGreater(
            sentiment_reading(survivors_only)["prior_limit_up_premium_pct"], 9.0)


class StageClassificationTests(unittest.TestCase):
    def test_an_icepoint_needs_every_condition_not_just_a_weak_one(self):
        icepoint = {"broken_rate": 0.6, "max_board_height": 2,
                    "promotion_rate": 0.1, "high_board_count": 1}
        self.assertEqual(classify_stage(icepoint), "icepoint")
        # A high ladder disqualifies it however bad the rest reads.
        self.assertNotEqual(classify_stage({**icepoint, "max_board_height": 6}), "icepoint")

    def test_a_climax_needs_every_condition_too(self):
        climax = {"broken_rate": 0.1, "max_board_height": 8,
                  "promotion_rate": 0.7, "high_board_count": 6}
        self.assertEqual(classify_stage(climax), "climax")
        self.assertNotEqual(classify_stage({**climax, "broken_rate": 0.35}), "climax")

    def test_a_missing_reading_cannot_satisfy_a_threshold(self):
        # Absent inputs must not be treated as passing; an unknown tape is not
        # a calm one.
        self.assertEqual(classify_stage({"broken_rate": None, "max_board_height": 2,
                                         "promotion_rate": 0.1, "high_board_count": 1}),
                         DEFAULT_STAGE)

    def test_an_ordinary_day_is_reported_as_mixed_not_forced_into_a_stage(self):
        self.assertEqual(classify_stage({"broken_rate": 0.35, "max_board_height": 4,
                                         "promotion_rate": 0.3, "high_board_count": 3}),
                         DEFAULT_STAGE)


if __name__ == "__main__":
    unittest.main()
