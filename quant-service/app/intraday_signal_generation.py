"""Deterministic signal generation used inside the intraday scan transaction.

This module deliberately owns no database, provider, clock or delivery state.
It composes the already point-in-time inputs that the scanner froze before any
signal event is persisted, so replay and production share the same candidate
generation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class IntradaySignalGenerationDependencies:
    base_rules: Callable[..., list[dict[str, Any]]]
    shadow_signal: Callable[..., dict[str, Any] | None]
    rebound_signal: Callable[..., dict[str, Any] | None]
    rebound_failure_signal: Callable[..., dict[str, Any] | None]
    eac_acceptance: Callable[..., dict[str, Any]]


def generate_intraday_signals(
    *,
    watch: dict[str, Any],
    symbol: str,
    quote: dict[str, Any] | None,
    previous_quote: dict[str, Any] | None,
    daily_factors: dict[str, Any],
    minute_features: dict[str, Any] | None,
    peer_context: dict[str, Any] | None,
    shadow_prior: dict[str, Any] | None,
    rebound_prior: dict[str, Any] | None,
    first_eac: dict[str, Any] | None,
    observed_at: Any,
    dependencies: IntradaySignalGenerationDependencies,
) -> list[dict[str, Any]]:
    """Create candidates from frozen scan inputs without changing their scores.

    State transitions, policy/risk gates, order-book attribution and durable
    event writes remain in the scanner.  This boundary only keeps the five
    independent candidate families in one deterministic, testable place.
    """
    rule_quote = {**quote, "_scan_observed_at": observed_at} if quote else None
    signals = list(dependencies.base_rules(
        watch, rule_quote, previous_quote, daily_factors, minute_features, peer_context,
    ))
    shadow = dependencies.shadow_signal(watch, quote, minute_features, peer_context, shadow_prior)
    if shadow is not None:
        signals.append(shadow)
    rebound = dependencies.rebound_signal(watch, quote, minute_features, peer_context, rebound_prior)
    if rebound is not None:
        signals.append(rebound)
    rebound_failure = dependencies.rebound_failure_signal(
        watch, quote, minute_features, peer_context, rebound_prior,
    )
    if rebound_failure is not None:
        signals.append(rebound_failure)
    if first_eac is not None:
        acceptance = dependencies.eac_acceptance(
            dict(first_eac["conditions"] or {}), first_observed_at=first_eac["observed_at"],
            observed_at=observed_at, quote=quote, previous_quote=previous_quote,
            minute_features=minute_features, peer_context=peer_context,
        )
        if acceptance["status"] in {"candidate", "attention_only"}:
            entry = acceptance["status"] == "candidate"
            signals.append({
                "symbol": symbol,
                "signal_key": (f"{symbol}:entry:upside_acceptance_eac_v4" if entry
                               else f"{symbol}:watch:upside_acceptance_attention_v4"),
                "signal_type": "entry" if entry else "watch",
                "severity": "warning" if entry else "info",
                "score": acceptance["score"], "hard": False,
                "independent_confirmation": True, "stage_upgrade": True,
                "conditions": {
                    "price": (quote or {}).get("price"),
                    "pct_change": (quote or {}).get("pct_change"),
                    "volume_ratio": (quote or {}).get("volume_ratio"),
                    "turnover_rate": (quote or {}).get("turnover_rate"),
                    "main_net_inflow": (quote or {}).get("main_net_inflow"),
                    "setup": "eac_acceptance_confirmed",
                    "eac_state": acceptance["status"],
                    "eac_acceptance_assessment": acceptance,
                    "minute_features": minute_features or {"status": "not_available"},
                    "peer_context": peer_context or {"status": "not_available"},
                },
                "risk_flags": ["eac_timed_acceptance", "manual_review_required", "no_automatic_order",
                               *acceptance.get("risk_flags", [])],
            })
    return signals


__all__ = ["IntradaySignalGenerationDependencies", "generate_intraday_signals"]
