"""Pure research-only microstructure observations for bounded watchlists."""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _levels(value: Any) -> list[dict[str, float]]:
    rows = value if isinstance(value, list) else []
    parsed: list[dict[str, float]] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        price, size = _number(row.get("price")), _number(row.get("size"))
        if price is not None and price > 0 and size is not None and size >= 0:
            parsed.append({"price": price, "size": size})
    return parsed


def order_book_observation(current: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute QI and a top-of-book OFI approximation without a trade claim."""
    bids, asks = _levels(current.get("bids")), _levels(current.get("asks"))
    if not bids or not asks:
        return {"status": "invalid_book"}
    bid1, ask1 = bids[0], asks[0]
    denominator = bid1["size"] + ask1["size"]
    qi1 = (bid1["size"] - ask1["size"]) / denominator if denominator else None
    weights = [1.0, 0.6065, 0.3679, 0.2231, 0.1353]
    bid_weighted = sum(weight * row["size"] for weight, row in zip(weights, bids, strict=False))
    ask_weighted = sum(weight * row["size"] for weight, row in zip(weights, asks, strict=False))
    qi5 = (bid_weighted - ask_weighted) / (bid_weighted + ask_weighted) if bid_weighted + ask_weighted else None
    result: dict[str, Any] = {
        "status": "observed", "qi1": round(qi1, 6) if qi1 is not None else None,
        "qi5": round(qi5, 6) if qi5 is not None else None,
        "bid_depth_lot": round(bid_weighted, 4), "ask_depth_lot": round(ask_weighted, 4),
        "book_spread": round(ask1["price"] - bid1["price"], 6),
        "book_mid": round((ask1["price"] + bid1["price"]) / 2, 6),
        "feature_version": "tencent-order-book-observation-v1",
    }
    if not previous:
        return {**result, "delta_status": "first_snapshot"}
    previous_bids, previous_asks = _levels(previous.get("bids")), _levels(previous.get("asks"))
    if not previous_bids or not previous_asks:
        return {**result, "delta_status": "missing_previous_book"}
    previous_bid, previous_ask = previous_bids[0], previous_asks[0]
    # Cont-style best-level order-flow imbalance; it is an observation
    # approximation because public snapshots do not expose every book event.
    bid_flow = (bid1["size"] if bid1["price"] >= previous_bid["price"] else 0.0) - (previous_bid["size"] if bid1["price"] <= previous_bid["price"] else 0.0)
    ask_flow = -(ask1["size"] if ask1["price"] <= previous_ask["price"] else 0.0) + (previous_ask["size"] if ask1["price"] >= previous_ask["price"] else 0.0)
    previous_volume = _number(previous.get("cumulative_volume_lot"))
    current_volume = _number(current.get("cumulative_volume_lot"))
    previous_amount = _number(previous.get("cumulative_amount"))
    current_amount = _number(current.get("cumulative_amount"))
    volume_delta = max(0.0, (current_volume or 0.0) - (previous_volume or 0.0)) if current_volume is not None and previous_volume is not None else None
    amount_delta = max(0.0, (current_amount or 0.0) - (previous_amount or 0.0)) if current_amount is not None and previous_amount is not None else None
    result.update({
        "delta_status": "ready", "ofi_best_level": round(bid_flow + ask_flow, 4),
        "cumulative_volume_delta_lot": volume_delta, "cumulative_amount_delta": amount_delta,
        "interval_vwap": round(amount_delta / (volume_delta * 100), 6) if volume_delta and amount_delta is not None else None,
        "outer_inner_delta_lot": (
            max(0.0, (_number(current.get("outer_volume_lot")) or 0.0) - (_number(previous.get("outer_volume_lot")) or 0.0))
            - max(0.0, (_number(current.get("inner_volume_lot")) or 0.0) - (_number(previous.get("inner_volume_lot")) or 0.0))
        ),
    })
    return result
