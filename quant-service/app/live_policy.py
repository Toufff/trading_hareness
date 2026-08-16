"""Pure pre-confirmation safety gate for intraday research signals.

The gate may suppress/downgrade a signal but never creates one.  It is kept
separate from setup rules so live scanning and future event replay use the
same market/data/tradability semantics.
"""

from __future__ import annotations

from typing import Any


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def live_policy_gate(signal: dict[str, Any], watch: dict[str, Any], quote: dict[str, Any] | None,
                     daily_factors: dict[str, Any] | None, market_context: dict[str, Any] | None,
                     fast_confirmation: dict[str, Any] | None,
                     portfolio_risk: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an explainable fail-closed policy decision.

    P0 consumes inputs already in the live scan plus the read-only paper
    ledger risk snapshot.  It still does not authorize a broker order;
    portfolio drawdown/strategy-health promotion remains a later research
    gate.
    """
    signal_type = str(signal.get("signal_type") or "watch")
    entry_like = signal_type == "entry"
    exit_like = signal_type in {"exit", "reduce"}
    reasons: list[str] = []
    flags: list[str] = []
    market_context = market_context if isinstance(market_context, dict) else {}
    factors = daily_factors if isinstance(daily_factors, dict) else {}
    fast = fast_confirmation if isinstance(fast_confirmation, dict) else {}
    portfolio = portfolio_risk if isinstance(portfolio_risk, dict) else {}
    price = _number((quote or {}).get("price"))
    if price is None or price <= 0:
        reasons.append("missing_live_price")
        flags.append("policy_data_unavailable")
    # Sina and the cross-sectional Tencent snapshot remain valuable evidence,
    # but only the same-scan Tencent watch batch has the explicit per-symbol
    # freshness contract used by a human-facing confirmation.  Do not let a
    # fallback silently become a decision source merely because it has a price.
    quote_source = str((quote or {}).get("price_source") or "unknown")
    quote_freshness = (quote or {}).get("price_freshness")
    quote_freshness = quote_freshness if isinstance(quote_freshness, dict) else {}
    if quote_source != "tencent_batched_watch_quote":
        reasons.append("quote_source_not_decision_eligible")
        flags.append("policy_quote_source_not_decision_eligible")
    elif str(quote_freshness.get("status") or "missing_timestamp") != "fresh":
        reasons.append("quote_source_timestamp_not_fresh")
        flags.append("policy_quote_timestamp_not_fresh")

    market_state = str(market_context.get("market_state") or "unknown")
    board_age = _number(market_context.get("board_snapshot_age_seconds"))
    market_context_status = str(market_context.get("status") or "missing")
    if entry_like and market_context_status != "available":
        reasons.append("market_context_missing")
        flags.append("policy_market_context_missing")
    if entry_like and market_state == "broad_risk_off":
        reasons.append("broad_risk_off_blocks_new_entry")
        flags.append("policy_market_risk_off")
    if entry_like and board_age is not None and board_age > 360:
        reasons.append("market_context_stale")
        flags.append("policy_market_context_stale")

    constraints = factors.get("trade_constraints") if isinstance(factors.get("trade_constraints"), dict) else {}
    if bool(constraints.get("is_suspended")):
        reasons.append("suspended_security")
        flags.append("policy_suspended")
    limit_up = _number(constraints.get("limit_up"))
    limit_down = _number(constraints.get("limit_down"))
    if entry_like and price is not None and limit_up is not None and price >= limit_up:
        reasons.append("limit_up_may_be_unbuyable")
        flags.append("policy_limit_up")
    if exit_like and price is not None and limit_down is not None and price <= limit_down:
        reasons.append("limit_down_may_be_unsellable")
        flags.append("policy_limit_down")

    if fast.get("status") == "mismatch":
        reasons.append("cross_source_price_mismatch")
        flags.append("policy_quote_mismatch")

    daily_status = str(factors.get("status") or "missing")
    if entry_like and daily_status in {"missing", "data_quality_blocked", "insufficient_history", "stale"}:
        reasons.append("daily_factor_quality_blocked")
        flags.append("policy_daily_factor_quality")
    if entry_like and portfolio.get("allowed") is False:
        portfolio_reasons = portfolio.get("reasons") if isinstance(portfolio.get("reasons"), list) else []
        reasons.extend(str(item) for item in portfolio_reasons if str(item))
        portfolio_flags = portfolio.get("risk_flags") if isinstance(portfolio.get("risk_flags"), list) else []
        flags.extend(str(item) for item in portfolio_flags if str(item))
        flags.append("policy_portfolio_risk")

    available_quantity = int(watch.get("available_quantity") or 0)
    risk_alert_only = bool(exit_like and watch.get("entry_price") is not None and available_quantity <= 0)
    if risk_alert_only:
        reasons.append("no_confirmed_sellable_quantity")
        flags.append("policy_risk_alert_only")

    blocks_confirmation = bool(reasons) and not risk_alert_only
    return {
        "version": "live-policy-gate-v1",
        "decision": "risk_alert_only" if risk_alert_only else "watch_only" if blocks_confirmation else "pass",
        "allow_confirmation": not blocks_confirmation,
        "reason_codes": reasons,
        "risk_flags": flags,
        "market_state": market_state,
        "board_snapshot_age_seconds": board_age,
        "quote_source": quote_source,
        "available_quantity": available_quantity,
        "portfolio_risk": portfolio,
        "scope": "P0 market/data/static-tradability plus paper-ledger concentration risk; no broker order",
    }


__all__ = ["live_policy_gate"]
