"""Pure intraday signal confirmation and de-duplication policy.

This module intentionally has no database, HTTP client, clock or provider
dependency.  The live scanner supplies timestamps and previously persisted
alert evidence; historical replay can call the same functions with its event
clock.  A policy decision never creates a signal and never authorizes an
order.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


CONFIRMATION_WINDOW = timedelta(minutes=5)
ALERT_COOLDOWN = timedelta(minutes=10)


def number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def signal_material_change(signal: dict[str, Any], prior_alert: dict[str, Any] | None) -> bool:
    """Return whether a persistent setup deserves a fresh interruption."""
    if signal.get("stage_upgrade") or prior_alert is None:
        return bool(signal.get("stage_upgrade"))
    previous_conditions = prior_alert.get("conditions") if isinstance(prior_alert.get("conditions"), dict) else {}
    current_conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    previous_score = number(prior_alert.get("score"))
    current_score = number(signal.get("score"))
    if previous_score is not None and current_score is not None and current_score - previous_score >= 10:
        return True
    previous_price = number(previous_conditions.get("price"))
    current_price = number(current_conditions.get("price"))
    if previous_price and current_price and abs(current_price / previous_price - 1) >= 0.01:
        return True
    previous_volume = number(previous_conditions.get("volume_ratio"))
    current_volume = number(current_conditions.get("volume_ratio"))
    if previous_volume is not None and current_volume is not None and current_volume - previous_volume >= 0.8:
        return True
    previous_flow = number(previous_conditions.get("main_net_inflow"))
    current_flow = number(current_conditions.get("main_net_inflow"))
    return bool(previous_flow is not None and current_flow is not None and previous_flow * current_flow < 0)


def signal_event_state(signal: dict[str, Any], *, observed_at: datetime,
                       latest_event_at: datetime | None, last_key_alerted_at: datetime | None,
                       last_symbol_watch_alerted_at: datetime | None,
                       last_key_alert: dict[str, Any] | None = None,
                       confirmation_window: timedelta = CONFIRMATION_WINDOW,
                       alert_cooldown: timedelta = ALERT_COOLDOWN) -> str:
    """Keep event confirmation distinct from alert de-duplication."""
    recent = latest_event_at is not None and observed_at - latest_event_at <= confirmation_window
    material_change = signal_material_change(signal, last_key_alert)
    key_duplicate = last_key_alerted_at is not None and recent and not material_change
    symbol_watch_duplicate = (
        signal["signal_type"] == "watch" and not signal.get("stage_upgrade")
        and last_symbol_watch_alerted_at is not None
        and observed_at - last_symbol_watch_alerted_at <= alert_cooldown
    )
    if key_duplicate or symbol_watch_duplicate:
        return "suppressed"
    if signal.get("alert_on_first_observation") and latest_event_at is None:
        return "confirmed"
    if signal["hard"] or signal.get("independent_confirmation") or recent:
        return "confirmed"
    return "confirming"


__all__ = ["ALERT_COOLDOWN", "CONFIRMATION_WINDOW", "number", "signal_event_state", "signal_material_change"]
