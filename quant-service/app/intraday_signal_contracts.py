"""Versioned, serialisable Insight contracts for persisted intraday signals.

The live rules intentionally keep returning small dictionaries: they are easy
to test and do not know about persistence.  This adapter adds the stable
``SignalSpec`` audit envelope immediately before an event is written.  It is
not a scorer, a probability model, or an order instruction.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .episode_lifecycle import signal_direction, strategy_family
from .strategy_contracts import EvidenceRef, SignalSpec, contract_payload


CONTRACT_VERSION = "intraday-signal-spec-v1"
CONFIRMATION_VALIDITY = timedelta(minutes=5)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _invalidation_codes(signal_type: str) -> tuple[str, ...]:
    if signal_type == "entry":
        return (
            "vwap_loss_with_negative_momentum",
            "peer_or_flow_confirmation_lost",
            "market_or_portfolio_policy_block",
        )
    if signal_type == "reduce":
        return (
            "vwap_reclaim_with_flow_repair",
            "t_plus_one_or_limit_down_may_prevent_execution",
        )
    if signal_type == "exit":
        return (
            "cross_source_price_recheck_required",
            "sellable_quantity_or_limit_down_may_prevent_execution",
        )
    if signal_type == "watch":
        return ("second_observation_or_independent_peer_confirmation_missing",)
    return ("quote_evidence_unavailable",)


def _evidence_refs(conditions: dict[str, Any], observed_at: datetime) -> tuple[EvidenceRef, ...]:
    """Describe only evidence already embedded in the persisted event.

    The envelope records the local availability time.  It never invents an
    upstream exchange timestamp for a provider which did not supply one.
    """
    policy = _mapping(conditions.get("policy_gate"))
    refs: list[EvidenceRef] = [
        EvidenceRef(
            source=str(policy.get("quote_source") or "watch_quote"),
            observed_at=observed_at,
            available_at=observed_at,
            fields=("price", "pct_change", "volume_ratio", "turnover_rate"),
            quality="decision_eligible" if policy.get("allow_confirmation") else "policy_constrained",
        )
    ]
    if conditions.get("main_net_inflow") is not None:
        refs.append(EvidenceRef(
            source="public_flow_proxy", observed_at=observed_at, available_at=observed_at,
            fields=("main_net_inflow", "main_flow_percentile"), quality="provider_estimate",
        ))
    minute = _mapping(conditions.get("minute_features"))
    if minute and minute.get("status") != "not_available":
        refs.append(EvidenceRef(
            source="intraday_minute_session", observed_at=observed_at, available_at=observed_at,
            fields=("return_1m_pct", "return_3m_pct", "above_vwap_pct", "minute_volume_multiple"),
            quality="available" if minute.get("status") in (None, "ready") else str(minute.get("status")),
        ))
    peers = _mapping(conditions.get("peer_context"))
    if peers and int(peers.get("requested_peer_count") or 0) > 0:
        refs.append(EvidenceRef(
            source="point_in_time_watchlist_membership", observed_at=observed_at, available_at=observed_at,
            fields=("available_peer_count", "confirming_peer_count", "confirming_breadth"),
            quality="available" if int(peers.get("available_peer_count") or 0) else "mapping_or_peer_missing",
        ))
    order_book = _mapping(conditions.get("order_book_proxy"))
    if str(order_book.get("status") or "") == "observed":
        age = order_book.get("latest_age_seconds")
        quality = "attribution_only"
        if isinstance(age, (int, float)) and age > 15:
            quality = "stale_attribution_only"
        refs.append(EvidenceRef(
            source="tencent_order_book_snapshot", observed_at=observed_at, available_at=observed_at,
            fields=("qi5", "ofi_30s", "ofi_1m", "seal_erosion_ratio_5m", "book_spread"),
            quality=quality,
        ))
    if isinstance(conditions.get("daily_rebound_state"), dict):
        refs.append(EvidenceRef(
            source="prior_completed_daily_rebound_state", observed_at=observed_at, available_at=observed_at,
            fields=("state", "model_score", "trained_at"), quality="prior_session_only",
        ))
    return tuple(refs)


def signal_contract(signal: dict[str, Any], observed_at: datetime) -> dict[str, Any]:
    """Build an immutable-style audit contract without mutating ``signal``."""
    conditions = _mapping(signal.get("conditions"))
    signal_type = str(signal.get("signal_type") or "watch")
    signal_key = str(signal.get("signal_key") or "unknown")
    contract = SignalSpec(
        strategy_key=strategy_family(signal_key),
        strategy_version=str(signal.get("strategy_version") or "live-research-contract-v1"),
        signal_type=signal_type,
        symbol=str(signal.get("symbol") or ""),
        direction=signal_direction(signal),
        observed_at=observed_at,
        score=float(signal.get("score") or 0.0),
        evidence=_evidence_refs(conditions, observed_at),
        risk_flags=tuple(str(item) for item in signal.get("risk_flags") or () if str(item)),
        conditions={
            "contract_version": CONTRACT_VERSION,
            "source_signal_key": signal_key,
            "setup": conditions.get("setup"),
            "policy_version": _mapping(conditions.get("policy_gate")).get("version"),
            "factor_contract_version": conditions.get("factor_contract_version"),
        },
        horizon_key="30m" if signal_type != "data_issue" else None,
        valid_until=observed_at + CONFIRMATION_VALIDITY,
        expected_return=None,
        probability_profile_id=None,
        invalidation_codes=_invalidation_codes(signal_type),
    )
    return contract_payload(contract)


__all__ = ["CONFIRMATION_VALIDITY", "CONTRACT_VERSION", "signal_contract"]
