"""Pure close-only sector-flow and Dragon-Tiger research features."""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def sector_flow_feature(
    current: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    prior: dict[str, Any] | None,
    rank_percentile: float | None,
    sign_streak: int,
    lhb: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe daily flow migration without mixing it into minute evidence."""
    net = _number(current.get("net_amount"))
    previous_net = _number((previous or {}).get("net_amount"))
    prior_net = _number((prior or {}).get("net_amount"))
    net_change = net - previous_net if net is not None and previous_net is not None else None
    net_acceleration = (
        net - 2.0 * previous_net + prior_net
        if net is not None and previous_net is not None and prior_net is not None else None
    )
    change_pct = _number(current.get("change_pct"))
    if net is None:
        transition = "insufficient"
    elif previous_net is not None and previous_net <= 0 < net:
        transition = "reversal_in"
    elif previous_net is not None and previous_net >= 0 > net:
        transition = "reversal_out"
    elif net > 0 and net_change is not None and net_change > 0:
        transition = "acceleration_in"
    elif net < 0 and net_change is not None and net_change < 0:
        transition = "acceleration_out"
    elif net > 0:
        transition = "persistent_in"
    elif net < 0:
        transition = "persistent_out"
    else:
        transition = "flat"
    divergence = None
    if change_pct is not None and net is not None:
        if change_pct > 0 and net < 0:
            divergence = "price_up_flow_out"
        elif change_pct < 0 and net > 0:
            divergence = "price_down_flow_in"
    lhb_payload = lhb or {}
    lhb_count = int(lhb_payload.get("stock_count") or 0)
    lhb_negative_count = int(lhb_payload.get("negative_count") or 0)
    return {
        "net_amount": net,
        "previous_net_amount": previous_net,
        "net_change_amount": net_change,
        "net_acceleration": net_acceleration,
        "rank_percentile": rank_percentile,
        "flow_sign_streak": sign_streak,
        "transition": transition,
        "price_flow_divergence": divergence,
        "lhb_stock_count": lhb_count,
        "lhb_net_amount": _number(lhb_payload.get("net_amount")),
        "lhb_negative_count": lhb_negative_count,
        "lhb_sell_pressure_ratio": lhb_negative_count / lhb_count if lhb_count else None,
        "limit_up_count": int(lhb_payload.get("limit_up_count") or 0),
        "research_only": True,
        "live_strategy_effect": "none",
    }


def sector_flow_outcome(
    transition: str,
    entry_close: Any,
    exit_close: Any,
    *,
    cross_section_median_return: float | None = None,
) -> dict[str, Any]:
    """Evaluate a saved close state after a later close; never an entry fill."""
    entry = _number(entry_close)
    exit_value = _number(exit_close)
    if entry is None or exit_value is None or entry <= 0:
        return {"status": "unavailable", "raw_return": None, "excess_return": None, "directional_return": None}
    raw_return = exit_value / entry - 1.0
    direction = 1 if transition in {"reversal_in", "acceleration_in", "persistent_in"} else -1 if transition in {
        "reversal_out", "acceleration_out", "persistent_out",
    } else 0
    return {
        "status": "matured",
        "raw_return": raw_return,
        "excess_return": raw_return - cross_section_median_return if cross_section_median_return is not None else None,
        "directional_return": raw_return * direction if direction else None,
        "expected_direction": direction,
    }


__all__ = ["sector_flow_feature", "sector_flow_outcome"]
