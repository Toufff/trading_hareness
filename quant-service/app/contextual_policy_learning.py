"""Offline, evidence-only review for the intraday alert policy.

This is deliberately *not* an execution-time reinforcement learner.  It turns
already delivered alerts into context/action/reward observations after the
market closes, applies conservative cohort gates, and leaves every live rule
and threshold untouched.  That makes the eventual policy research reproducible
from the signal and outcome ledgers rather than from a mutable online model.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any


POLICY_LEARNING_MODEL_VERSION = "contextual-bandit-offline-v1"
POLICY_MIN_MATURED_SIGNALS = 200
POLICY_MIN_TRADING_DAYS = 60
POLICY_MIN_COHORT_SIGNALS = 30


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _attribution(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return evidence.get("attribution") if isinstance(evidence.get("attribution"), dict) else {}


def _action(row: dict[str, Any]) -> str:
    attribution = _attribution(row)
    return ":".join((
        str(row.get("signal_type") or "unknown"),
        str(attribution.get("model_version") or "legacy-unversioned"),
        str(attribution.get("stage") or "generic"),
    ))


def _context(row: dict[str, Any]) -> str:
    attribution = _attribution(row)
    return ":".join((
        str(attribution.get("market_state") or "unknown"),
        str(attribution.get("sector_linkage") or "unobserved"),
        str(attribution.get("microstructure_state") or "unobserved"),
    ))


def _reward(row: dict[str, Any]) -> tuple[float, float] | None:
    """Return raw and downside-penalized reward in basis points.

    ``raw_return`` and MAE have already been directionalized by the outcome
    ledger, so entry/watch and reduce/exit can share the same review.  The
    second number is intentionally a diagnostic risk penalty, not a trading
    score or a threshold input.
    """
    raw_return = _number(row.get("raw_return"))
    if raw_return is None:
        return None
    mae = min(0.0, _number(row.get("maximum_adverse_excursion")) or 0.0)
    raw_bps = raw_return * 10_000
    return raw_bps, raw_bps + mae * 2_500


def contextual_bandit_policy_review(rows: list[dict[str, Any]], *, focus_exchange_date: str | None = None) -> dict[str, Any]:
    """Summarize mature 30-minute outcomes as offline policy evidence.

    ``rows`` must contain one row per delivered signal with its mature 30m
    outcome, if any.  The returned action values are withheld below the cohort
    gate to prevent a handful of alerts from being mistaken for a learned
    policy.
    """
    mature = [row for row in rows if str(row.get("status")) == "matured" and _reward(row) is not None]
    dates = {str(row.get("exchange_date")) for row in mature if row.get("exchange_date")}
    gate_ready = len(mature) >= POLICY_MIN_MATURED_SIGNALS and len(dates) >= POLICY_MIN_TRADING_DAYS
    action_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    context_action_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in mature:
        action_rows[_action(row)].append(row)
        context_action_rows[(_context(row), _action(row))].append(row)

    def summarize(key: str, items: list[dict[str, Any]], *, context: str | None = None) -> dict[str, Any]:
        rewards = [_reward(item) for item in items]
        raw = [value[0] for value in rewards if value is not None]
        risk_adjusted = [value[1] for value in rewards if value is not None]
        reviewable = gate_ready and len(raw) >= POLICY_MIN_COHORT_SIGNALS
        result: dict[str, Any] = {
            "action": key,
            "matured_signals": len(raw),
            "status": "reviewable_offline_only" if reviewable else "descriptive_only",
            "minimum_cohort_signals": POLICY_MIN_COHORT_SIGNALS,
        }
        if context is not None:
            result["context"] = context
        # Do not surface a pseudo-value below the guardrail.  It remains in
        # the immutable outcome ledger and can be recomputed once enough data
        # exists, but cannot silently steer tomorrow's live rules.
        if reviewable:
            result["mean_directional_reward_bps"] = round(mean(raw), 2)
            result["mean_downside_penalized_reward_bps"] = round(mean(risk_adjusted), 2)
        return result

    actions = [summarize(action, items) for action, items in action_rows.items()]
    contexts = [summarize(action, items, context=context)
                for (context, action), items in context_action_rows.items()]
    actions.sort(key=lambda item: (-int(item["matured_signals"]), str(item["action"])))
    contexts.sort(key=lambda item: (-int(item["matured_signals"]), str(item["context"]), str(item["action"])))
    today = [row for row in rows if focus_exchange_date and str(row.get("exchange_date")) == focus_exchange_date]
    today_mature = [row for row in today if str(row.get("status")) == "matured" and _reward(row) is not None]
    return {
        "model_version": POLICY_LEARNING_MODEL_VERSION,
        "mode": "offline_context_action_reward_review",
        "policy_update": "disabled",
        "validation_gate": {
            "status": "ready_for_offline_policy_review" if gate_ready else "accumulating",
            "matured_unique_signals": len(mature),
            "trading_days": len(dates),
            "required_unique_signals": POLICY_MIN_MATURED_SIGNALS,
            "required_trading_days": POLICY_MIN_TRADING_DAYS,
        },
        "action_values": actions,
        "context_action_values": contexts,
        "daily_review": {
            "exchange_date": focus_exchange_date,
            "delivered_signals": len(today),
            "matured_30m_signals": len(today_mature),
            "pending_30m_signals": len(today) - len(today_mature),
            "actions": sorted({_action(row) for row in today}),
        },
    }
