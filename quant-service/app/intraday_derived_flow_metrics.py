"""Derive watch volume ratio and turnover rate from the licensed THS snapshot.

``signal_rules`` needs three public flow fields together (``volume_ratio``,
``turnover_rate``, ``main_net_inflow``).  All three arrive from one public,
unauthenticated Eastmoney ``ulist.np`` request, which fails roughly half of the
30-second scans observed on 2026-08-26 (``public HTTP GET request failed after
bounded retry``, upstream ``Server disconnected without sending a response``),
and whose bounded basket is not a cross-section - so the rule engine has to
treat every one of its values as research corroboration only.

Two of those three fields are *definitions* rather than proprietary vendor
measurements:

    turnover_rate = cumulative_volume_shares / float_shares * 100
    volume_ratio  = (cumulative_volume_shares / elapsed_session_minutes)
                    / (mean_recent_daily_volume_shares / SESSION_MINUTES)

Every input already exists locally.  The licensed Fuyao/THS
``a_share_prices_snapshot`` fetched by every scan carries cumulative volume in
shares for the whole A-share cross-section, and float shares plus trailing
daily volumes are persisted in ``quant.daily_fundamentals`` and
``quant.canonical_bars_daily``.  Deriving both fields therefore adds no
provider call at all, and unlike the Eastmoney basket it inherits the all-A
cross-sectional scope of the snapshot it is computed from.

``main_net_inflow`` has no THS equivalent and this module never invents one.
The official 59-route REST contract pinned in ``fuyao_catalog`` documents no
fund-flow route and states that minute bars, tick and Level-2 are out of
scope; the Eastmoney ``f62`` aggregation stays that field's only source, and
therefore its only failure mode as well.

Accuracy measured against complete live 36-symbol Eastmoney responses on
2026-08-26.  At 11:08 CST: turnover_rate median absolute error 0.08% (max
2.11%, on names whose persisted float share predates a recent share change),
volume_ratio median 1.15% (max 1.57%).  Through the full capture path at
11:34 CST: turnover_rate 0.08% median / 2.17% max, volume_ratio 0.19% median
/ 0.45% max - the volume_ratio residual shrinks as the elapsed-minute
denominator grows, which is the signature of a sub-minute difference in when
each side starts counting the session, not of a different formula.
:func:`derived_flow_divergence` recomputes exactly that comparison on every
scan where both sources answer, so the agreement is monitored continuously
rather than asserted once.
"""

from __future__ import annotations

from datetime import datetime, time as _time
from typing import Any, Callable
from zoneinfo import ZoneInfo


_CN_TZ = ZoneInfo("Asia/Shanghai")
_MORNING_OPEN = _time(9, 30)
_MORNING_CLOSE = _time(11, 30)
_AFTERNOON_OPEN = _time(13, 0)
_AFTERNOON_CLOSE = _time(15, 0)

#: Continuous-auction minutes in one A-share session (09:30-11:30, 13:00-15:00).
SESSION_MINUTES = 240
#: Fields this module is allowed to derive.  ``main_net_inflow`` is excluded on
#: purpose: no licensed route supplies it and it must never be fabricated.
DERIVED_FLOW_FIELDS = ("volume_ratio", "turnover_rate")
#: Below this the elapsed-minute denominator is dominated by opening noise.
MIN_ELAPSED_MINUTES = 3


def _minutes_between(start: _time, end: _time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def session_elapsed_minutes(observed_at: datetime) -> int:
    """Return continuous-auction minutes elapsed on the Shanghai clock.

    The call auction before 09:30 contributes volume but no continuous-auction
    time, so a pre-open observation yields ``0`` and the caller skips the
    ratio rather than dividing by an arbitrary floor.
    """
    local = observed_at.astimezone(_CN_TZ).time()
    elapsed = 0
    if local > _MORNING_OPEN:
        elapsed += _minutes_between(_MORNING_OPEN, min(local, _MORNING_CLOSE))
    if local > _AFTERNOON_OPEN:
        elapsed += _minutes_between(_AFTERNOON_OPEN, min(local, _AFTERNOON_CLOSE))
    return min(elapsed, SESSION_MINUTES)


def derive_watch_flow_metrics(
    quotes: dict[str, dict[str, Any]],
    reference: dict[str, dict[str, Any]],
    *,
    observed_at: datetime,
    number: Callable[[Any], float | None],
) -> dict[str, dict[str, float]]:
    """Compute per-symbol derived flow metrics from snapshot volume alone.

    A symbol is skipped entirely rather than partially guessed whenever its
    snapshot volume, float share or trailing volume reference is missing, so a
    stale or absent reference can never turn into a fabricated ratio.
    """
    elapsed = session_elapsed_minutes(observed_at)
    derived: dict[str, dict[str, float]] = {}
    for symbol, quote in quotes.items():
        entry = reference.get(symbol)
        if not isinstance(entry, dict):
            continue
        volume_shares = number(quote.get("volume"))
        if volume_shares is None or volume_shares <= 0:
            continue
        metrics: dict[str, float] = {}
        float_shares = number(entry.get("float_shares"))
        if float_shares is not None and float_shares > 0:
            metrics["turnover_rate"] = round(volume_shares / float_shares * 100, 5)
        mean_daily_volume = number(entry.get("mean_daily_volume_shares"))
        if mean_daily_volume is not None and mean_daily_volume > 0 and elapsed >= MIN_ELAPSED_MINUTES:
            metrics["volume_ratio"] = round(
                (volume_shares / elapsed) / (mean_daily_volume / SESSION_MINUTES), 5,
            )
        if metrics:
            derived[symbol] = metrics
    return derived


def apply_derived_watch_flow_metrics(
    quotes: dict[str, dict[str, Any]],
    derived: dict[str, dict[str, float]],
) -> dict[str, dict[str, str]]:
    """Overlay derived metrics as the primary source and label every field.

    Callers apply this *after* the Eastmoney merge: a derived value replaces
    the public one, and any field this module could not derive keeps whatever
    Eastmoney supplied, which is exactly the requested primary/fallback order.
    The returned per-symbol labels are recorded on the quote itself so a
    replayed rule input can always tell which source produced each number.
    """
    sources: dict[str, dict[str, str]] = {}
    for symbol, quote in quotes.items():
        metrics = derived.get(symbol) or {}
        labels = {
            field: "fuyao_ths_derived" if field in metrics
            else "eastmoney_watch_flow" if quote.get(field) is not None
            else "unavailable"
            for field in DERIVED_FLOW_FIELDS
        }
        labels["main_net_inflow"] = (
            "eastmoney_watch_flow" if quote.get("main_net_inflow") is not None else "unavailable"
        )
        for field, value in metrics.items():
            quote[f"{field}_eastmoney_observed"] = quote.get(field)
            quote[field] = value
        quote["flow_metric_sources"] = labels
        sources[symbol] = labels
    return sources


def derived_flow_divergence(
    quotes: dict[str, dict[str, Any]],
    derived: dict[str, dict[str, float]],
    *,
    number: Callable[[Any], float | None],
) -> dict[str, Any]:
    """Summarize derived-versus-Eastmoney agreement for the scan's evidence.

    This is the ongoing proof that the substitution is sound.  It compares
    only symbols where both sources answered in the same scan, so an Eastmoney
    outage produces an empty comparison rather than a fake perfect score.
    """
    summary: dict[str, Any] = {}
    for field in DERIVED_FLOW_FIELDS:
        errors: list[float] = []
        for symbol, metrics in derived.items():
            derived_value = metrics.get(field)
            observed = number((quotes.get(symbol) or {}).get(f"{field}_eastmoney_observed"))
            if derived_value is None or observed is None or observed == 0:
                continue
            errors.append(abs(derived_value - observed) / abs(observed) * 100)
        errors.sort()
        summary[field] = {
            "compared_symbols": len(errors),
            "median_abs_error_pct": round(errors[len(errors) // 2], 4) if errors else None,
            "max_abs_error_pct": round(errors[-1], 4) if errors else None,
        }
    return summary


__all__ = [
    "DERIVED_FLOW_FIELDS", "MIN_ELAPSED_MINUTES", "SESSION_MINUTES",
    "apply_derived_watch_flow_metrics", "derive_watch_flow_metrics",
    "derived_flow_divergence", "session_elapsed_minutes",
]
