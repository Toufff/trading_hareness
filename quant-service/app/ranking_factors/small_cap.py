"""Relative size evidence. No market calls, strategy gates or order decisions."""
from bisect import bisect_left, bisect_right
import hashlib
import json
from math import isfinite, log2

VERSION = 'size-preference-soft-band-2026-09-11'


def band_score(total_mv, lower=100e8, upper=600e8):
    """User size preference, not a claim that 600bn is universally small-cap.

    Flat inside the soft target band; half score one doubling outside either
    boundary. Total market cap is the declared size axis. Ordinary float is
    disclosed, not a second reward for tightly locked newly listed companies.
    """
    value = positive(total_mv)
    if value is None:
        return 0.
    distance = log2(lower/value) if value < lower else log2(value/upper) if value > upper else 0.
    return 100 * 2 ** (-distance * distance)


def positive(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def context(universe, as_of_date):
    """Universe is the caller's same-day eligible reference cohort, not top picks.

    Existing Longhu screen snapshots use CNY total_mv/circ_mv. circ_mv is NOT
    free float. Both fields and the exact reference date are required. Do not
    carry forward a stale cap, treat zero as microcap, or rank among top5 only.
    """
    day = as_of_date.replace('-', '')
    values = {}
    for symbol, row in universe.items():
        total, floating = positive(row.get('total_mv')), positive(row.get('circ_mv'))
        if (str(row.get('trade_date', '')).replace('-', '') == day and total and floating
                and floating <= total * 1.01):
            values[symbol] = (total, floating)
    n = len(values)
    coverage = n / max(1, len(universe))
    ready = n >= 20 and coverage >= .8
    result = dict(version=VERSION, as_of_date=as_of_date,
                  status='ready' if ready else 'insufficient_coverage',
                  universe_count=len(universe), valid_count=n, coverage=coverage,
                  scope='本轮具有完整历史的主板股票；非全A市值排名',
                  unit='CNY', cap_basis='总市值100—600亿元软偏好；普通流通市值仅披露，不是自由流通市值',
                  preferred_total_cap_yi=[100,600], half_score_total_cap_yi=[50,1200],
                  reference_role='分位仅用于解释与数据质检，不参与大小偏好得分', minimum_reference_count=20,
                  minimum_coverage=.8,
                  source_sha256=hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest(),
                  evidence={})
    if not ready:
        return result
    totals = sorted(v[0] for v in values.values())
    floats = sorted(v[1] for v in values.values())

    def percentile(v, sample):
        return (bisect_left(sample, v) + bisect_right(sample, v)) / (2 * n)

    for symbol, (total, floating) in sorted(values.items()):
        tp, fp = percentile(total, totals), percentile(floating, floats)
        result['evidence'][symbol] = dict(total_mv=total, circ_mv=floating,
              total_percentile=tp, float_percentile=fp,
              score=round(band_score(total), 4), float_ratio=round(floating/total,6),
              explanation=f'总市值{total/1e8:.2f}亿元，流通市值{floating/1e8:.2f}亿元；'
                          f'总市值100—600亿元范围得100分、两侧平滑衰减；规模偏好{band_score(total):.2f}分；非越小越好')
    return result
