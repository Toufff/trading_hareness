"""Immutable, minimal inputs for replaying the pure intraday rule function.

Live scans already persist quotes and emitted signals.  They did not preserve
the complete *non-signal* input set, which makes a later replay unable to
distinguish "the rule did not fire" from "the evidence was never recorded".
The contract below captures only fields consumed by ``signal_rules`` and its
upside assessment.  It intentionally excludes provider raw payloads and does
not create a trading instruction.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


INTRADAY_RULE_INPUT_SCHEMA_VERSION = "intraday-rule-input-v1"

_WATCH_FIELDS = (
    "symbol", "alert_on_entry", "alert_on_exit", "entry_price", "available_quantity",
    "hard_stop", "take_profit", "metadata",
)
_QUOTE_FIELDS = (
    "symbol", "price", "pct_change", "volume_ratio", "turnover_rate", "main_net_inflow",
    "main_flow_percentile", "price_source", "price_freshness",
)
_PREVIOUS_QUOTE_FIELDS = ("symbol", "source_name", "price", "pct_change", "observed_at")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _selected(value: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {field: _json_value(value.get(field)) for field in fields if field in value}


def intraday_rule_input_payload(*, watch: dict[str, Any], quote: dict[str, Any] | None,
                                previous_quote: dict[str, Any] | None,
                                daily_factors: dict[str, Any] | None,
                                minute_features: dict[str, Any] | None,
                                peer_context: dict[str, Any] | None,
                                model_version: str) -> dict[str, Any]:
    """Freeze exactly the causal inputs used by the pure entry/watch rules."""
    return {
        "schema_version": INTRADAY_RULE_INPUT_SCHEMA_VERSION,
        "model_version": str(model_version),
        "watch": _selected(watch, _WATCH_FIELDS) or {},
        "quote": _selected(quote, _QUOTE_FIELDS),
        "previous_quote": _selected(previous_quote, _PREVIOUS_QUOTE_FIELDS),
        "daily_factors": _json_value(daily_factors or {"status": "not_available"}),
        "minute_features": _json_value(minute_features or {"status": "not_available"}),
        "peer_context": _json_value(peer_context or {"status": "not_available"}),
    }


def intraday_rule_input_hash(payload: dict[str, Any]) -> str:
    """Hash canonical JSON so a replay input set is immutable and auditable."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def intraday_rule_replay_inputs(payload: dict[str, Any], *, expected_model_version: str | None = None) -> dict[str, Any]:
    """Validate a stored contract before a caller reruns the pure rule function."""
    if str(payload.get("schema_version") or "") != INTRADAY_RULE_INPUT_SCHEMA_VERSION:
        raise ValueError("unsupported intraday rule input schema")
    model_version = str(payload.get("model_version") or "")
    if expected_model_version and model_version != expected_model_version:
        raise ValueError("intraday rule input model version does not match replay model")
    watch = payload.get("watch")
    if not isinstance(watch, dict) or not watch.get("symbol"):
        raise ValueError("intraday rule input requires a watch symbol")
    for key in ("daily_factors", "minute_features", "peer_context"):
        if not isinstance(payload.get(key), dict):
            raise ValueError(f"intraday rule input requires object {key}")
    return {
        "watch": dict(watch), "quote": dict(payload["quote"]) if isinstance(payload.get("quote"), dict) else None,
        "previous_quote": (dict(payload["previous_quote"])
                           if isinstance(payload.get("previous_quote"), dict) else None),
        "daily_factors": dict(payload["daily_factors"]), "minute_features": dict(payload["minute_features"]),
        "peer_context": dict(payload["peer_context"]), "model_version": model_version,
    }


__all__ = [
    "INTRADAY_RULE_INPUT_SCHEMA_VERSION", "intraday_rule_input_hash", "intraday_rule_input_payload",
    "intraday_rule_replay_inputs",
]
