"""Descriptive intraday setup states shared by alerts and future replay.

This is intentionally not an order engine.  It assigns a reproducible state
from contemporaneous evidence so live scans, later outcome attribution and
historical replay can discuss the same setup without changing any live rule
threshold.  A state is allowed to *explain* an existing signal but never to
create or promote one by itself.
"""

from __future__ import annotations

import math
from typing import Any


STATE_MACHINE_VERSION = "intraday-setup-state-v1"


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def classify_setup_state(
    watch: dict[str, Any], quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify only current evidence; never infer an unobserved trade fill."""
    quote = quote or {}
    minute = minute_features or {}
    peers = peer_context or {}
    policy = policy or {}
    price = _number(quote.get("price"))
    return_3m = _number(minute.get("return_3m_pct"))
    above_vwap = _number(minute.get("above_vwap_pct"))
    volume_multiple = max(
        _number(quote.get("volume_ratio")) or 0.0,
        _number(minute.get("minute_volume_multiple")) or 0.0,
    )
    flow = _number(quote.get("main_net_inflow"))
    available_peers = int(peers.get("available_peer_count") or 0)
    confirming_peers = int(peers.get("confirming_peer_count") or 0)
    reasons: list[str] = []
    if price is None or price <= 0:
        return _payload("data_blocked", ["missing_live_price"], quote, minute, peers)
    if str(policy.get("decision") or "") in {"watch_only", "risk_alert_only"}:
        reasons.extend(str(item) for item in policy.get("reason_codes") or [])
        return _payload("policy_constrained", reasons or ["live_policy_constraint"], quote, minute, peers)
    if bool((watch.get("metadata") or {}).get("suspended")):
        return _payload("non_tradable", ["watch_metadata_suspended"], quote, minute, peers)
    if return_3m is None or above_vwap is None:
        return _payload("evidence_incomplete", ["minute_feature_missing"], quote, minute, peers)

    entry_price = _number(watch.get("entry_price"))
    peer_loss = available_peers >= 2 and confirming_peers == 0
    if entry_price and entry_price > 0:
        return_since_entry = (price / entry_price - 1) * 100
        if (above_vwap <= -0.15 and return_3m <= -0.50 and (flow is None or flow <= 0 or peer_loss)) or (
            return_since_entry <= -2.5 and (above_vwap < 0 or return_3m < 0)
        ):
            return _payload("acceptance_failure", ["vwap_acceptance_lost", "negative_short_horizon_momentum"], quote, minute, peers,
                            return_since_entry_pct=return_since_entry)

    peer_confirmation = confirming_peers >= 2
    if return_3m >= 0.8 and above_vwap >= 0 and volume_multiple >= 1.8 and (flow is None or flow > 0 or peer_confirmation):
        return _payload("continuation_acceptance", ["positive_momentum", "above_vwap", "relative_volume", "flow_or_peer_confirmation"],
                        quote, minute, peers)
    if return_3m >= 0.5 and above_vwap >= 0 and volume_multiple >= 1.5:
        return _payload("rebound_acceptance", ["positive_reclaim", "above_vwap", "volume_confirmation_pending"], quote, minute, peers)
    if above_vwap < 0 and return_3m < 0:
        return _payload("weakening", ["below_vwap", "negative_short_horizon_momentum"], quote, minute, peers)
    return _payload("neutral", ["no_confirmed_continuation_or_failure"], quote, minute, peers)


def _payload(
    state: str, reasons: list[str], quote: dict[str, Any], minute: dict[str, Any], peers: dict[str, Any],
    *, return_since_entry_pct: float | None = None,
) -> dict[str, Any]:
    return {
        "version": STATE_MACHINE_VERSION,
        "state": state,
        "reasons": reasons,
        "metrics": {
            "price": _number(quote.get("price")),
            "return_3m_pct": _number(minute.get("return_3m_pct")),
            "above_vwap_pct": _number(minute.get("above_vwap_pct")),
            "minute_volume_multiple": _number(minute.get("minute_volume_multiple")),
            "volume_ratio": _number(quote.get("volume_ratio")),
            "main_net_inflow": _number(quote.get("main_net_inflow")),
            "available_peer_count": int(peers.get("available_peer_count") or 0),
            "confirming_peer_count": int(peers.get("confirming_peer_count") or 0),
            "return_since_entry_pct": round(return_since_entry_pct, 4) if return_since_entry_pct is not None else None,
        },
        "scope": "descriptive_evidence_only_no_order_or_threshold_change",
    }


__all__ = ["STATE_MACHINE_VERSION", "classify_setup_state"]
