"""Out-of-sample splitting and selection-bias-corrected Sharpe ratios.

Every strategy conclusion this system has produced was read off the same data
it was formed on. That is not a small caveat at the number of variants being
compared: over two sessions a single mode read +4.75%, then -1.32%, then
+1.65% as its sample changed, and each figure was reported as a finding. Under
enough looks, the best-looking variant is the luckiest one, and nothing here
could tell the difference.

Two tools address different halves of that:

``walk_forward_splits`` never lets a window be judged on days that helped
choose it, and holds an embargo between train and test because an outcome
settled on the following session overlaps the day after its own window - the
purging López de Prado introduced for exactly this overlap.

``deflated_sharpe_ratio`` asks whether a Sharpe survives the number of trials
that produced it. Its null is the expected maximum Sharpe of that many
independent attempts, so a result only clears it by beating what selection
alone would have handed back.

Both take returns already net of costs; a Sharpe on gross returns answers a
question nobody can trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from statistics import NormalDist, mean, pstdev
from typing import Sequence

#: Euler-Mascheroni, in the expected-maximum term of the deflated Sharpe.
EULER_MASCHERONI = 0.5772156649015329
#: Trading sessions per year, for reporting an annualised figure alongside.
SESSIONS_PER_YEAR = 244
#: Below this many observations a Sharpe is not evaluated at all.
#:
#: The deflation alone does not protect against a tiny sample: three tightly
#: clustered wins produce an enormous Sharpe that clears any trial count,
#: which is exactly the shape of the three-observation mode readings this
#: system kept reporting as findings.  The sample floor is the guard; the
#: deflation handles selection among samples that are large enough to judge.
MINIMUM_EVALUABLE_OBSERVATIONS = 20


@dataclass(frozen=True)
class WalkForwardSplit:
    """One train window, the gap that protects it, and its test window."""

    train: tuple[date, ...]
    test: tuple[date, ...]
    embargo: tuple[date, ...]


def walk_forward_splits(sessions: Sequence[date], *, train_size: int, test_size: int,
                        embargo: int = 0) -> list[WalkForwardSplit]:
    """Roll a train/test pair forward over ordered trading sessions.

    ``embargo`` sessions sit between train and test and belong to neither.
    Without it the last training day and the first test day share an outcome
    window - a signal on the final training session settles against the first
    test session's bar - and the split leaks the answer it is testing.

    Windows are expressed in sessions rather than calendar days so a holiday
    cannot silently shrink one.
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    if embargo < 0:
        raise ValueError("embargo cannot be negative")
    ordered = sorted(set(sessions))
    splits: list[WalkForwardSplit] = []
    start = 0
    while True:
        train_end = start + train_size
        test_start = train_end + embargo
        test_end = test_start + test_size
        if test_end > len(ordered):
            break
        splits.append(WalkForwardSplit(
            train=tuple(ordered[start:train_end]),
            embargo=tuple(ordered[train_end:test_start]),
            test=tuple(ordered[test_start:test_end]),
        ))
        start += test_size
    return splits


def newey_west_mean_t_stat(values: Sequence[float], *, bandwidth: int) -> tuple[float | None, float | None]:
    """Newey-West HAC standard error and t-stat for a series' sample mean.

    An h-day-forward return or IC series sampled every trading day is
    autocorrelated by construction: consecutive h-day windows overlap for
    h>1 sessions, so a plain iid standard error understates the true
    variance and overstates significance (the López de Prado purging /
    Newey-West 1987 correction). ``bandwidth`` is conventionally ``h - 1``
    for an h-day-forward series; ``bandwidth=0`` reduces to the plain iid
    variance, so this is a safe drop-in for a 1-day series too.
    """
    count = len(values)
    if count < 2:
        return None, None
    average = mean(values)
    centered = [value - average for value in values]
    bandwidth = max(0, int(bandwidth))
    long_run_variance = sum(value * value for value in centered) / count
    for lag in range(1, min(bandwidth, count - 1) + 1):
        weight = 1 - lag / (bandwidth + 1)
        autocovariance = sum(centered[index] * centered[index - lag] for index in range(lag, count)) / count
        long_run_variance += 2 * weight * autocovariance
    long_run_variance = max(long_run_variance, 0.0)
    if long_run_variance <= 0:
        return 0.0, None
    standard_error = math.sqrt(long_run_variance / count)
    return standard_error, (average / standard_error if standard_error > 0 else None)


def sharpe_ratio(returns: Sequence[float]) -> float | None:
    """Per-observation Sharpe of a return series, or None when undefined.

    Not annualised: the deflated Sharpe below is defined on the per-observation
    figure, and scaling one and not the other is the easiest way to get a
    confident wrong answer.
    """
    if len(returns) < 2:
        return None
    deviation = pstdev(returns)
    return mean(returns) / deviation if deviation > 0 else None


def annualised(value: float, *, sessions_per_year: int = SESSIONS_PER_YEAR) -> float:
    """Scale a per-observation Sharpe to a yearly one, for reporting only."""
    return value * math.sqrt(sessions_per_year)


def _moments(returns: Sequence[float]) -> tuple[float, float]:
    """Skewness and non-excess kurtosis; normal values when there is no spread."""
    deviation = pstdev(returns)
    if deviation <= 0:
        return 0.0, 3.0
    average = mean(returns)
    skew = sum(((value - average) / deviation) ** 3 for value in returns) / len(returns)
    kurtosis = sum(((value - average) / deviation) ** 4 for value in returns) / len(returns)
    return skew, kurtosis


def probabilistic_sharpe_ratio(returns: Sequence[float], *, benchmark: float = 0.0) -> float | None:
    """Probability the true Sharpe exceeds ``benchmark``.

    Corrects for the skew and fat tails that make a short A-share series look
    steadier than it is: a strategy of many small gains and rare large losses
    inflates a plain Sharpe exactly where the plain Sharpe is least reliable.
    """
    observed = sharpe_ratio(returns)
    if observed is None:
        return None
    count = len(returns)
    skew, kurtosis = _moments(returns)
    variance = 1 - skew * observed + (kurtosis - 1) / 4 * observed ** 2
    if variance <= 0:
        return None
    statistic = (observed - benchmark) * math.sqrt(count - 1) / math.sqrt(variance)
    return NormalDist().cdf(statistic)


def expected_maximum_sharpe(*, trials: int, trial_sharpe_variance: float) -> float:
    """The Sharpe selection alone is expected to produce from ``trials`` looks.

    This is the null a candidate must beat. It rises with the number of
    variants compared, which is why counting the trials honestly matters more
    than the test itself: undercount them and everything passes.
    """
    if trials < 2 or trial_sharpe_variance <= 0:
        return 0.0
    normal = NormalDist()
    return math.sqrt(trial_sharpe_variance) * (
        (1 - EULER_MASCHERONI) * normal.inv_cdf(1 - 1 / trials)
        + EULER_MASCHERONI * normal.inv_cdf(1 - 1 / (trials * math.e))
    )


def deflated_sharpe_ratio(returns: Sequence[float], *, trials: int,
                          trial_sharpe_variance: float,
                          minimum_observations: int = MINIMUM_EVALUABLE_OBSERVATIONS,
                          ) -> float | None:
    """Probability the Sharpe survives the selection that produced it.

    ``trials`` is every variant compared, not only the ones written down -
    each mode, each threshold tried, each window. ``trial_sharpe_variance`` is
    the variance of the Sharpe ratios across those trials.

    Returns None below ``minimum_observations``, because deflation does not
    rescue a sample too small to carry a Sharpe: three clustered wins produce
    one large enough to clear any trial count. Refusing to answer is the
    correct answer there, and it is the one this system was not giving.

    Read the value as a probability: below ~0.95 the result is not
    distinguishable from the best of that many coin flips.
    """
    if len(returns) < max(2, minimum_observations):
        return None
    null = expected_maximum_sharpe(trials=trials, trial_sharpe_variance=trial_sharpe_variance)
    return probabilistic_sharpe_ratio(returns, benchmark=null)


__all__ = [
    "EULER_MASCHERONI", "MINIMUM_EVALUABLE_OBSERVATIONS", "SESSIONS_PER_YEAR",
    "WalkForwardSplit", "annualised",
    "deflated_sharpe_ratio", "expected_maximum_sharpe", "newey_west_mean_t_stat", "probabilistic_sharpe_ratio",
    "sharpe_ratio", "walk_forward_splits",
]
