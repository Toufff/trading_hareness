"""Pure multiscale market-volume and board-flow research features.

The functions in this module intentionally do not perform I/O.  Intraday
Eastmoney flow, whole-market quote snapshots and close-only Tushare evidence
remain separate inputs; callers may persist the resulting labelled evidence,
but it must not change live strategy thresholds before the research gate is met.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Any, Iterable


MIN_CONCEPT_COVERAGE = 300


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _ratio_change(current: Any, previous: Any) -> float | None:
    current_value, previous_value = _number(current), _number(previous)
    if current_value is None or previous_value in (None, 0):
        return None
    return round((current_value / previous_value - 1.0) * 100.0, 6)


def board_flow_breadth(
    items: Iterable[dict[str, Any]],
    taxonomy_key: str = "eastmoney_concept",
) -> dict[str, Any]:
    """Summarise one same-source board snapshot without treating missing as zero."""
    rows: list[tuple[float, float | None]] = []
    for item in items:
        if item.get("taxonomy_key") != taxonomy_key:
            continue
        flow = _number(item.get("net_inflow"))
        if flow is None:
            continue
        rows.append((flow, _number(item.get("change_pct"))))
    flows = [flow for flow, _ in rows]
    changes = [change for _, change in rows if change is not None]
    count = len(rows)
    return {
        "taxonomy_key": taxonomy_key,
        "board_count": count,
        "positive_count": sum(flow > 0 for flow in flows),
        "negative_count": sum(flow < 0 for flow in flows),
        "zero_count": sum(flow == 0 for flow in flows),
        "positive_ratio": round(sum(flow > 0 for flow in flows) / count, 6) if count else None,
        "median_flow": round(median(flows), 6) if flows else None,
        "mean_flow": round(mean(flows), 6) if flows else None,
        "mean_change_pct": round(mean(changes), 6) if changes else None,
    }


def intraday_flow_state(
    current: dict[str, Any],
    *,
    five_minute_reference: dict[str, Any] | None = None,
    session_reference: dict[str, Any] | None = None,
    afternoon_min_positive_ratio: float | None = None,
    minimum_coverage: int = MIN_CONCEPT_COVERAGE,
) -> dict[str, Any]:
    """Classify expansion, risk-off and repair from one-minute flow breadth."""
    flags: list[str] = []
    board_count = int(current.get("board_count") or 0)
    positive_ratio = _number(current.get("positive_ratio"))
    if board_count < minimum_coverage:
        flags.append("concept_coverage_below_minimum")
    if positive_ratio is None:
        flags.append("positive_flow_breadth_missing")

    def delta(reference: dict[str, Any] | None) -> float | None:
        reference_ratio = _number((reference or {}).get("positive_ratio"))
        if positive_ratio is None or reference_ratio is None:
            return None
        return round(positive_ratio - reference_ratio, 6)

    five_minute_delta = delta(five_minute_reference)
    session_delta = delta(session_reference)
    afternoon_repair = (
        round(positive_ratio - afternoon_min_positive_ratio, 6)
        if positive_ratio is not None and afternoon_min_positive_ratio is not None else None
    )

    state = "insufficient"
    if not flags:
        if positive_ratio >= 0.55 and (five_minute_delta is None or five_minute_delta >= -0.03):
            state = "flow_expansion"
        elif positive_ratio <= 0.20 and (five_minute_delta is None or five_minute_delta <= 0.03):
            state = "flow_risk_off"
        elif afternoon_repair is not None and afternoon_repair >= 0.12 and positive_ratio < 0.50:
            state = "late_repair"
        elif five_minute_delta is not None and five_minute_delta >= 0.10:
            state = "flow_acceleration"
        elif five_minute_delta is not None and five_minute_delta <= -0.10:
            state = "flow_deterioration"
        else:
            state = "mixed_rotation"
    return {
        **current,
        "state": state,
        "five_minute_positive_ratio_delta": five_minute_delta,
        "session_positive_ratio_delta": session_delta,
        "afternoon_repair_strength": afternoon_repair,
        "minimum_concept_coverage": minimum_coverage,
        "quality_flags": flags,
        "research_only": True,
    }


def volume_flow_regime(
    current_summary: dict[str, Any],
    *,
    previous_close_summary: dict[str, Any] | None = None,
    concept_flow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a midday/close whole-market snapshot with board-flow breadth."""
    previous = previous_close_summary or {}
    amount_change = _ratio_change(current_summary.get("market_amount"), previous.get("market_amount"))
    volume_change = _ratio_change(current_summary.get("market_volume"), previous.get("market_volume"))
    advancers = _number(current_summary.get("advancers"))
    decliners = _number(current_summary.get("decliners"))
    unchanged = _number(current_summary.get("unchanged")) or 0.0
    breadth_denominator = (advancers or 0.0) + (decliners or 0.0) + unchanged
    advancer_ratio = round((advancers or 0.0) / breadth_denominator, 6) if breadth_denominator else None
    concept_positive_ratio = _number((concept_flow or {}).get("positive_ratio"))
    flags: list[str] = []
    if amount_change is None or volume_change is None:
        flags.append("previous_close_volume_baseline_missing")
    if advancer_ratio is None:
        flags.append("market_breadth_missing")
    if concept_positive_ratio is None:
        flags.append("concept_flow_breadth_missing")

    state = "insufficient"
    if not flags:
        expanding = amount_change >= 5.0 or volume_change >= 5.0
        contracting = amount_change <= -5.0 and volume_change <= -3.0
        broad_positive = advancer_ratio >= 0.55 and concept_positive_ratio >= 0.50
        broad_negative = advancer_ratio <= 0.35 and concept_positive_ratio <= 0.25
        if expanding and broad_positive:
            state = "risk_expansion"
        elif expanding and broad_negative:
            state = "distribution"
        elif contracting and advancer_ratio >= 0.40 and concept_positive_ratio < 0.50:
            state = "weak_repair"
        elif contracting and broad_negative:
            state = "passive_decline"
        else:
            state = "neutral_rotation"
    return {
        "state": state,
        "market_amount": _number(current_summary.get("market_amount")),
        "market_volume": _number(current_summary.get("market_volume")),
        "amount_change_pct": amount_change,
        "volume_change_pct": volume_change,
        "advancer_ratio": advancer_ratio,
        "median_change_pct": _number(current_summary.get("median_change_pct")),
        "concept_positive_ratio": concept_positive_ratio,
        "concept_board_count": int((concept_flow or {}).get("board_count") or 0),
        "quality_flags": flags,
        "research_only": True,
    }


POOL_EVENT_TYPES = frozenset({
    "limit_up_pool", "previous_limit_pool", "limit_open_pool",
    "limit_down_pool", "sub_new_limit_pool", "strong_pool", "auction_final", "limit_chain",
})


def market_event_identity_key(
    provider: str,
    event_type: str,
    symbol: str,
    occurred_date: str,
) -> str | None:
    """Return the stable identity for mutable limit-pool snapshots only."""
    if event_type not in POOL_EVENT_TYPES:
        return None
    return f"{provider}:{event_type}:{symbol}:{occurred_date}"


__all__ = [
    "MIN_CONCEPT_COVERAGE", "POOL_EVENT_TYPES", "board_flow_breadth",
    "intraday_flow_state", "market_event_identity_key", "volume_flow_regime",
]
