"""Coverage for out-of-sample splitting and selection-corrected Sharpe ratios.

Each test pins a way the previous approach could have reported a finding that
was not one. The system had no OOS split and no multiple-testing correction at
all, so every comparison between modes was decided on the data that produced
them.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
import unittest

from app.strategy_validation import (
    annualised,
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    newey_west_mean_t_stat,
    probabilistic_sharpe_ratio,
    sharpe_ratio,
    walk_forward_splits,
)


def _sessions(count, start=date(2026, 1, 5)):
    return [start + timedelta(days=index) for index in range(count)]


class WalkForwardSplitTests(unittest.TestCase):
    def test_train_and_test_never_share_a_session(self):
        for split in walk_forward_splits(_sessions(40), train_size=10, test_size=5):
            self.assertEqual(set(split.train) & set(split.test), set())

    def test_the_test_window_always_follows_the_train_window(self):
        for split in walk_forward_splits(_sessions(40), train_size=10, test_size=5):
            self.assertGreater(min(split.test), max(split.train))

    def test_the_embargo_sits_between_them_and_belongs_to_neither(self):
        split = walk_forward_splits(_sessions(40), train_size=10, test_size=5, embargo=2)[0]
        self.assertEqual(len(split.embargo), 2)
        self.assertGreater(min(split.embargo), max(split.train))
        self.assertLess(max(split.embargo), min(split.test))

    def test_without_an_embargo_the_windows_are_adjacent(self):
        # Documented rather than desirable: an outcome settled on the next
        # session makes the last training day overlap the first test day.
        split = walk_forward_splits(_sessions(40), train_size=10, test_size=5)[0]
        self.assertEqual(split.embargo, ())

    def test_windows_advance_by_one_test_length(self):
        splits = walk_forward_splits(_sessions(40), train_size=10, test_size=5)
        self.assertEqual(splits[1].test[0], splits[0].test[0] + timedelta(days=5))

    def test_a_series_too_short_for_one_window_yields_nothing(self):
        self.assertEqual(walk_forward_splits(_sessions(8), train_size=10, test_size=5), [])

    def test_duplicate_and_unordered_sessions_are_normalised(self):
        sessions = _sessions(20)
        shuffled = list(reversed(sessions)) + sessions[:3]
        splits = walk_forward_splits(shuffled, train_size=10, test_size=5)
        self.assertEqual(splits[0].train, tuple(sessions[:10]))

    def test_a_non_positive_window_is_rejected_rather_than_silently_empty(self):
        with self.assertRaises(ValueError):
            walk_forward_splits(_sessions(20), train_size=0, test_size=5)


class SharpeTests(unittest.TestCase):
    def test_a_flat_series_has_no_sharpe_rather_than_a_huge_one(self):
        self.assertIsNone(sharpe_ratio([0.1, 0.1, 0.1]))

    def test_one_observation_is_not_enough(self):
        self.assertIsNone(sharpe_ratio([0.1]))

    def test_annualising_scales_by_the_root_of_the_session_count(self):
        self.assertAlmostEqual(annualised(0.1, sessions_per_year=244),
                               0.1 * math.sqrt(244), places=9)


class ProbabilisticSharpeTests(unittest.TestCase):
    def test_a_clearly_positive_series_beats_a_zero_benchmark(self):
        returns = [0.01, 0.02, 0.015, 0.012, 0.018, 0.011] * 10
        self.assertGreater(probabilistic_sharpe_ratio(returns), 0.99)

    def test_a_series_around_zero_does_not(self):
        returns = [0.01, -0.01] * 30
        self.assertLess(probabilistic_sharpe_ratio(returns), 0.6)

    def test_a_higher_benchmark_lowers_the_probability(self):
        # A series with no losses saturates the normal CDF to 1.0 at every
        # benchmark, so the monotonicity it is meant to show disappears into
        # floating-point equality. A moderate Sharpe (~0.2 per period) keeps
        # the zero-benchmark probability off the ceiling so raising the bar
        # can visibly lower it.
        returns = [0.006, -0.004] * 30
        self.assertGreater(probabilistic_sharpe_ratio(returns, benchmark=0.0),
                           probabilistic_sharpe_ratio(returns, benchmark=1.0))

    def test_it_is_stricter_than_the_plain_sharpe_would_suggest(self):
        # The plain Sharpe of this series is large; the probability that the
        # true one is positive is what actually matters over 60 observations.
        returns = [0.01, 0.02, 0.015, 0.012, 0.018, 0.011] * 10
        self.assertGreater(sharpe_ratio(returns), 2.0)
        self.assertLessEqual(probabilistic_sharpe_ratio(returns), 1.0)

    def test_a_series_with_no_spread_has_no_probability(self):
        self.assertIsNone(probabilistic_sharpe_ratio([0.01] * 30))


class ExpectedMaximumSharpeTests(unittest.TestCase):
    def test_more_trials_raise_the_bar(self):
        few = expected_maximum_sharpe(trials=5, trial_sharpe_variance=0.25)
        many = expected_maximum_sharpe(trials=500, trial_sharpe_variance=0.25)
        self.assertGreater(many, few)

    def test_a_single_trial_has_no_selection_to_correct(self):
        self.assertEqual(expected_maximum_sharpe(trials=1, trial_sharpe_variance=0.25), 0.0)

    def test_identical_trials_leave_nothing_to_deflate(self):
        self.assertEqual(expected_maximum_sharpe(trials=50, trial_sharpe_variance=0.0), 0.0)


class DeflatedSharpeTests(unittest.TestCase):
    # A modest, realistic edge. A series with a very high Sharpe saturates the
    # normal CDF at 1.0 and every comparison below becomes 1.0 == 1.0.
    returns = [0.02, -0.01] * 30

    def test_it_is_never_more_generous_than_the_undeflated_probability(self):
        undeflated = probabilistic_sharpe_ratio(self.returns)
        deflated = deflated_sharpe_ratio(self.returns, trials=100, trial_sharpe_variance=0.25)
        self.assertLessEqual(deflated, undeflated)

    def test_counting_more_trials_makes_the_same_result_less_convincing(self):
        modest = deflated_sharpe_ratio(self.returns, trials=10, trial_sharpe_variance=0.25)
        heavy = deflated_sharpe_ratio(self.returns, trials=1000, trial_sharpe_variance=0.25)
        self.assertGreater(modest, heavy)

    def test_a_three_sample_mode_reading_is_refused_not_scored(self):
        # This is the shape that produced the +4.75% mode reading: three
        # clustered wins whose Sharpe is enormous precisely because there is
        # almost no spread. Deflation alone would pass it, so the sample floor
        # has to refuse it outright.
        thin = [0.05, 0.04, 0.06]
        self.assertGreater(sharpe_ratio(thin), 5.0)
        self.assertIsNone(deflated_sharpe_ratio(thin, trials=50, trial_sharpe_variance=0.25))

    def test_the_floor_is_where_a_sample_becomes_evaluable(self):
        from app.strategy_validation import MINIMUM_EVALUABLE_OBSERVATIONS
        returns = [0.01, 0.02, -0.01, 0.015] * 20
        just_under = returns[:MINIMUM_EVALUABLE_OBSERVATIONS - 1]
        just_over = returns[:MINIMUM_EVALUABLE_OBSERVATIONS]
        self.assertIsNone(deflated_sharpe_ratio(just_under, trials=10, trial_sharpe_variance=0.25))
        self.assertIsNotNone(deflated_sharpe_ratio(just_over, trials=10, trial_sharpe_variance=0.25))

    def test_an_unusable_series_reports_none_rather_than_a_number(self):
        self.assertIsNone(deflated_sharpe_ratio([0.01], trials=10, trial_sharpe_variance=0.25))


class NeweyWestMeanTStatTests(unittest.TestCase):
    def test_zero_bandwidth_matches_the_plain_iid_t_stat(self):
        values = [0.01, -0.02, 0.015, 0.03, -0.01, 0.02, -0.015, 0.025]
        se, t_stat = newey_west_mean_t_stat(values, bandwidth=0)
        n = len(values)
        average = sum(values) / n
        population_variance = sum((value - average) ** 2 for value in values) / n
        expected_se = math.sqrt(population_variance / n)
        self.assertAlmostEqual(se, expected_se, places=9)
        self.assertAlmostEqual(t_stat, average / expected_se, places=9)

    def test_positive_autocorrelation_widens_the_standard_error(self):
        # An h-day-forward series built by summing a 5-day rolling window of a
        # smooth underlying signal: consecutive windows share 4 of their 5
        # days, so they move together almost in lockstep (strong positive
        # autocorrelation) the way an actual overlapping h-day-forward return
        # series does.
        base = [math.sin(index / 3.0) * 0.02 for index in range(40)]
        overlapping = [sum(base[index:index + 5]) for index in range(len(base) - 4)]
        se_iid, _ = newey_west_mean_t_stat(overlapping, bandwidth=0)
        se_nw, _ = newey_west_mean_t_stat(overlapping, bandwidth=4)
        self.assertGreater(se_nw, se_iid, "ignoring the overlap understates the true standard error")

    def test_too_few_observations_is_undefined(self):
        self.assertEqual(newey_west_mean_t_stat([0.1], bandwidth=0), (None, None))

    def test_zero_variance_series_has_no_t_stat(self):
        se, t_stat = newey_west_mean_t_stat([0.01, 0.01, 0.01], bandwidth=1)
        self.assertEqual(se, 0.0)
        self.assertIsNone(t_stat)


if __name__ == "__main__":
    unittest.main()
