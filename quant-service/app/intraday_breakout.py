"""Pure breakout assessment and acceptance labels for intraday research."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .strategy_thresholds import MAX_ENTRY_INTRADAY_GAIN_PCT, STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT


def upside_research_assessment(
    quote: dict[str, Any] | None,
    daily_factors: dict[str, Any] | None,
    minute_features: dict[str, Any] | None,
    peer_context: dict[str, Any] | None,
    *,
    number: Callable[[Any], float | None],
    eac_window: Callable[[Any], str | None],
) -> dict[str, Any]:
    """Score point-in-time breakout ingredients without producing an order."""
    if not quote or not minute_features:
        return {"status": "insufficient_minute_data", "score": 0, "components": {}}
    pct_change = number(quote.get("pct_change"))
    return_1m = number(minute_features.get("return_1m_pct"))
    return_3m = number(minute_features.get("return_3m_pct"))
    volume_multiple = number(minute_features.get("minute_volume_multiple"))
    above_vwap = number(minute_features.get("above_vwap_pct"))
    breakout = number(minute_features.get("breakout_above_prior_high_pct"))
    range_position = number(minute_features.get("session_range_position"))
    flow_percentile = number(quote.get("main_flow_percentile"))
    peer_breadth = number((peer_context or {}).get("confirming_breadth")) or 0.0
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    available_peers = int((peer_context or {}).get("available_peer_count") or 0)
    session_window = eac_window(minute_features.get("time"))
    volume_profile = minute_features.get("time_bucket_volume_profile") if isinstance(minute_features.get("time_bucket_volume_profile"), dict) else {}
    volume_profile_ready = volume_profile.get("status") == "ready"
    volume_surprise = number(volume_profile.get("volume_surprise"))
    flow_confirmation = flow_percentile is not None and flow_percentile >= 0.8
    peer_confirmation = available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.5
    daily_base_ready = bool((daily_factors or {}).get("base_structure_ready"))
    components = {
        "entry_window": pct_change is not None
                        and STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT,
        "short_acceleration": return_3m is not None and 1.2 <= return_3m <= 3.5 and return_1m is not None and return_1m >= 0.25,
        "relative_volume": volume_multiple is not None and volume_multiple >= 2.5,
        "volume_not_extreme": volume_multiple is not None and volume_multiple <= 20.0,
        "volume_baseline_ready": volume_profile_ready,
        "time_bucket_volume_surprise": volume_surprise is not None and volume_surprise >= 2.0,
        "volume_confirmation": volume_profile_ready and volume_surprise is not None and volume_surprise >= 2.0,
        "above_vwap": above_vwap is not None and 0.2 <= above_vwap <= 5.5,
        "new_intraday_high": breakout is not None and breakout >= 0,
        "upper_range": range_position is not None and range_position >= 0.85,
        "flow_confirmation": flow_confirmation,
        "peer_confirmation": peer_confirmation,
        "flow_or_peers": flow_confirmation or peer_confirmation,
        "daily_trend_or_base": (daily_factors or {}).get("ma_trend") == "bullish" or daily_base_ready,
        "daily_base_ready": daily_base_ready,
        "fresh_session_window": session_window in {"morning", "afternoon"},
    }
    weights = {"entry_window": 8, "short_acceleration": 20, "relative_volume": 8,
               "time_bucket_volume_surprise": 10, "above_vwap": 14, "new_intraday_high": 14,
               "upper_range": 8, "flow_or_peers": 14, "daily_trend_or_base": 4,
               "fresh_session_window": 4}
    score = sum(weight for key, weight in weights.items() if components.get(key))
    volume_outlier = bool(components["relative_volume"] and not components["volume_not_extreme"])
    if volume_outlier:
        score = max(0, score - 12)
    core = ("entry_window", "short_acceleration", "relative_volume", "volume_confirmation",
            "above_vwap", "new_intraday_high", "flow_or_peers")
    candidate = score >= 76 and all(components[key] for key in core) and components["fresh_session_window"] and not volume_outlier
    attention_core = ("entry_window", "short_acceleration", "relative_volume", "above_vwap",
                      "new_intraday_high", "flow_or_peers")
    attention = all(components[key] for key in attention_core) and (volume_outlier or not components["fresh_session_window"] or not volume_profile_ready)
    return {
        "status": "candidate" if candidate else "attention_only" if attention else "not_confirmed",
        "score": score, "components": components,
        "metrics": {"pct_change": pct_change, "return_1m_pct": return_1m, "return_3m_pct": return_3m,
                    "minute_volume_multiple": volume_multiple, "above_vwap_pct": above_vwap,
                    "breakout_above_prior_high_pct": breakout, "session_range_position": range_position,
                    "main_flow_percentile": flow_percentile, "peer_breadth": peer_breadth,
                    "confirming_peer_count": confirming_peers, "available_peer_count": available_peers,
                    "session_window": session_window, "time_bucket_volume_surprise": volume_surprise,
                    "time_bucket_volume_profile": volume_profile},
        "risk_flags": (["relative_volume_outlier_requires_acceptance"] if volume_outlier else [])
                      + (["time_bucket_volume_baseline_insufficient"] if not volume_profile_ready else [])
                      + (["late_or_unknown_session_requires_stronger_confirmation"] if attention and not components["fresh_session_window"] else []),
    }


def eac_acceptance_assessment(
    first_conditions: dict[str, Any] | None, *,
    first_observed_at: datetime, observed_at: datetime,
    quote: dict[str, Any] | None, previous_quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    number: Callable[[Any], float | None], confirmation_window_seconds: float,
) -> dict[str, Any]:
    """Check whether a first expansion survived the configured holding window."""
    first_assessment = (first_conditions or {}).get("upside_research_assessment")
    if not isinstance(first_assessment, dict) or first_assessment.get("status") not in {"candidate", "attention_only"}:
        return {"status": "not_eligible", "score": 0, "components": {}, "metrics": {}}
    first_price = number((first_conditions or {}).get("price"))
    current_price = number((quote or {}).get("price"))
    previous_price = number((previous_quote or {}).get("price"))
    pct_change = number((quote or {}).get("pct_change"))
    above_vwap = number((minute_features or {}).get("above_vwap_pct"))
    range_position = number((minute_features or {}).get("session_range_position"))
    flow_percentile = number((quote or {}).get("main_flow_percentile"))
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    available_peers = int((peer_context or {}).get("available_peer_count") or 0)
    elapsed_seconds = max(0.0, (observed_at - first_observed_at).total_seconds())
    retained_from_first_pct = ((current_price / first_price - 1) * 100 if current_price is not None and first_price is not None and first_price > 0 else None)
    scan_return_pct = ((current_price / previous_price - 1) * 100 if current_price is not None and previous_price is not None and previous_price > 0 else None)
    components = {
        "minimum_hold_time": 30 <= elapsed_seconds <= confirmation_window_seconds,
        "entry_window": pct_change is not None
                        and STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT,
        "retains_expansion": retained_from_first_pct is not None and retained_from_first_pct >= -0.6,
        "no_fast_reversal": scan_return_pct is not None and scan_return_pct >= -0.25,
        "above_vwap": above_vwap is not None and above_vwap >= 0,
        "upper_session_range": range_position is not None and range_position >= 0.75,
        "flow_or_peers": ((flow_percentile is not None and flow_percentile >= 0.8) or (available_peers >= 2 and confirming_peers >= 2)),
    }
    accepted = all(components.values())
    baseline_ready = first_assessment.get("status") == "candidate"
    status = "candidate" if accepted and baseline_ready else "attention_only" if accepted else "not_confirmed"
    weights = {"minimum_hold_time": 15, "entry_window": 10, "retains_expansion": 20, "no_fast_reversal": 15,
               "above_vwap": 20, "upper_session_range": 10, "flow_or_peers": 10}
    return {
        "status": status, "score": sum(weight for key, weight in weights.items() if components[key]),
        "components": components,
        "metrics": {"elapsed_seconds": round(elapsed_seconds, 1), "first_price": first_price, "current_price": current_price,
                    "retained_from_first_pct": round(retained_from_first_pct, 4) if retained_from_first_pct is not None else None,
                    "scan_return_pct": round(scan_return_pct, 4) if scan_return_pct is not None else None,
                    "above_vwap_pct": above_vwap, "session_range_position": range_position,
                    "main_flow_percentile": flow_percentile, "confirming_peer_count": confirming_peers,
                    "available_peer_count": available_peers},
        "risk_flags": ([] if baseline_ready else ["time_bucket_volume_baseline_insufficient"])
                      + ([] if accepted else ["acceptance_not_confirmed"]),
    }


__all__ = ["eac_acceptance_assessment", "upside_research_assessment"]
