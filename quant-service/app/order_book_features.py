"""Pure research-only microstructure observations for bounded watchlists."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _levels(value: Any) -> list[dict[str, float] | None]:
    """Keep Tencent's level positions; never compact past an invalid quote."""
    rows = value if isinstance(value, list) else []
    parsed: list[dict[str, float] | None] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            parsed.append(None)
            continue
        price, size = _number(row.get("price")), _number(row.get("size"))
        parsed.append({"price": price, "size": size} if price is not None and price > 0 and size is not None and size >= 0 else None)
    return parsed


def _first_valid(levels: list[dict[str, float] | None]) -> dict[str, float] | None:
    return next((level for level in levels if level is not None), None)


def _weighted_depth(levels: list[dict[str, float] | None]) -> float:
    weights = (1.0, 0.6065, 0.3679, 0.2231, 0.1353)
    return sum(weight * level["size"] for index, level in enumerate(levels) if level is not None for weight in (weights[index],))


def order_book_observation(current: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute positional QI and top-of-book OFI without a trade claim."""
    bids, asks = _levels(current.get("bids")), _levels(current.get("asks"))
    bid1, ask1 = _first_valid(bids), _first_valid(asks)
    if bid1 is None and ask1 is None:
        return {"status": "invalid_book"}
    bid_weighted, ask_weighted = _weighted_depth(bids), _weighted_depth(asks)
    depth_total = bid_weighted + ask_weighted
    qi5 = (bid_weighted - ask_weighted) / depth_total if depth_total else None
    qi1 = ((bid1["size"] if bid1 else 0.0) - (ask1["size"] if ask1 else 0.0)) / ((bid1["size"] if bid1 else 0.0) + (ask1["size"] if ask1 else 0.0)) if bid1 or ask1 else None
    one_sided = bool(bid1) != bool(ask1)
    book_side = "bid_only" if bid1 and not ask1 else "ask_only" if ask1 and not bid1 else "two_sided"
    result: dict[str, Any] = {
        "status": "observed", "one_sided_book": one_sided, "book_side": book_side,
        "qi1": round(qi1, 6) if qi1 is not None else None,
        "qi5": round(qi5, 6) if qi5 is not None else None,
        "bid_depth_lot": round(bid_weighted, 4), "ask_depth_lot": round(ask_weighted, 4),
        "seal_volume_lot": bid1["size"] if book_side == "bid_only" else ask1["size"] if book_side == "ask_only" else None,
        "book_spread": round(ask1["price"] - bid1["price"], 6) if bid1 and ask1 else None,
        "book_mid": round((ask1["price"] + bid1["price"]) / 2, 6) if bid1 and ask1 else None,
        "feature_version": "tencent-order-book-observation-v2",
    }
    if not previous:
        return {**result, "delta_status": "first_snapshot"}
    previous_bids, previous_asks = _levels(previous.get("bids")), _levels(previous.get("asks"))
    previous_bid, previous_ask = _first_valid(previous_bids), _first_valid(previous_asks)
    if previous_bid is None and previous_ask is None:
        return {**result, "delta_status": "missing_previous_book"}
    previous_volume = _number(previous.get("cumulative_volume_lot"))
    current_volume = _number(current.get("cumulative_volume_lot"))
    previous_amount = _number(previous.get("cumulative_amount"))
    current_amount = _number(current.get("cumulative_amount"))
    volume_delta = max(0.0, current_volume - previous_volume) if current_volume is not None and previous_volume is not None else None
    amount_delta = max(0.0, current_amount - previous_amount) if current_amount is not None and previous_amount is not None else None
    # A sealed one-sided book has no opposing best quote, so Cont OFI is not
    # defined.  Retain the seal delta separately for later erosion studies.
    ofi: float | None = None
    if bid1 and ask1 and previous_bid and previous_ask:
        bid_flow = (bid1["size"] if bid1["price"] >= previous_bid["price"] else 0.0) - (previous_bid["size"] if bid1["price"] <= previous_bid["price"] else 0.0)
        ask_flow = -(ask1["size"] if ask1["price"] <= previous_ask["price"] else 0.0) + (previous_ask["size"] if ask1["price"] >= previous_ask["price"] else 0.0)
        ofi = bid_flow + ask_flow
    current_seal = result["seal_volume_lot"]
    previous_side = "bid_only" if previous_bid and not previous_ask else "ask_only" if previous_ask and not previous_bid else "two_sided"
    previous_seal = previous_bid["size"] if previous_side == "bid_only" else previous_ask["size"] if previous_side == "ask_only" else None
    result.update({
        "delta_status": "ready", "ofi_best_level": round(ofi, 4) if ofi is not None else None,
        "cumulative_volume_delta_lot": volume_delta, "cumulative_amount_delta": amount_delta,
        "interval_vwap": round(amount_delta / (volume_delta * 100), 6) if volume_delta and amount_delta and amount_delta > 0 else None,
        "outer_inner_delta_lot": (
            max(0.0, (_number(current.get("outer_volume_lot")) or 0.0) - (_number(previous.get("outer_volume_lot")) or 0.0))
            - max(0.0, (_number(current.get("inner_volume_lot")) or 0.0) - (_number(previous.get("inner_volume_lot")) or 0.0))
        ),
        "seal_volume_delta_lot": current_seal - previous_seal if current_seal is not None and previous_seal is not None and book_side == previous_side and book_side != "two_sided" else None,
    })
    return result


def aggregate_order_book_observations(rows: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any]:
    """Aggregate only already-persisted, same-session OFI observations.

    A three-second best-level imbalance is deliberately too noisy to label a
    signal.  The output retains 30-second, one-minute and five-minute sums;
    callers choose a label only when at least three valid snapshots exist.
    """
    ordered = sorted((row for row in rows if isinstance(row.get("observed_at"), datetime)
                      and isinstance(row.get("raw"), dict)), key=lambda row: row["observed_at"], reverse=True)
    latest = dict(ordered[0]["raw"].get("order_book_features") or {}) if ordered else {}
    result: dict[str, Any] = {"status": "missing", "latest_features": latest,
                              "feature_version": "tencent-order-book-aggregate-v1"}
    for label, seconds in (("30s", 30), ("1m", 60), ("5m", 300)):
        cutoff = observed_at - timedelta(seconds=seconds)
        features = [dict(row["raw"].get("order_book_features") or {}) for row in ordered if row["observed_at"] >= cutoff]
        values = [_number(feature.get("ofi_best_level")) for feature in features]
        valid_values = [value for value in values if value is not None]
        result[f"ofi_{label}"] = round(sum(valid_values), 4) if valid_values else None
        result[f"ofi_{label}_sample_count"] = len(valid_values)
        result[f"one_sided_{label}_count"] = sum(bool(feature.get("one_sided_book")) for feature in features)
    if ordered:
        result["status"] = "observed"
        result["latest_observed_at"] = ordered[0]["observed_at"].isoformat()
        result["latest_age_seconds"] = round(max(0.0, (observed_at - ordered[0]["observed_at"]).total_seconds()), 3)
    return result
