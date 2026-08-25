"""Pure explainable intraday signal rules for live scans and replay.

This module has no database, HTTP or alert side effects.
"""

from __future__ import annotations

from typing import Any, Callable

from .strategy_thresholds import (
    MAX_ENTRY_INTRADAY_GAIN_PCT,
    STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT,
    STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR,
)


def signal_rules(watch: dict[str, Any], quote: dict[str, Any] | None,
                           previous_quote: dict[str, Any] | None, daily_factors: dict[str, Any] | None = None,
                           minute_features: dict[str, Any] | None = None, peer_context: dict[str, Any] | None = None, *, number: Callable[[Any], float | None], upside_assessment_fn: Callable[..., dict[str, Any]], model_version: str,
                           opening_gap_window: bool = False) -> list[dict[str, Any]]:
    """Return explainable, non-executable signal conditions for one symbol.

    Entry signals deliberately require a second scan.  A configured hard stop
    is the only immediate exit condition, and only for an explicitly marked
    sellable position.  These are prompts for review, never order instructions.
    """
    symbol = str(watch["symbol"])
    if not quote or quote.get("price") is None:
        return [{"signal_key": f"{symbol}:data_issue:{model_version}", "signal_type": "data_issue",
                 "severity": "warning", "score": 0, "hard": False,
                 "conditions": {"quote_available": False}, "risk_flags": ["missing_tencent_quote"]}]
    price = float(quote["price"])
    pct_change = float(quote.get("pct_change") or 0)
    volume_ratio_value = number(quote.get("volume_ratio"))
    turnover_rate_value = number(quote.get("turnover_rate"))
    main_net_inflow_value = number(quote.get("main_net_inflow"))
    # Missing public cross-sectional flow is an availability state, not a
    # numeric zero.  Preserve it in frozen evidence so a quiet scan can be
    # diagnosed without silently turning a provider outage into "no inflow".
    volume_ratio = volume_ratio_value if volume_ratio_value is not None else 0.0
    turnover_rate = turnover_rate_value if turnover_rate_value is not None else 0.0
    main_net_inflow = main_net_inflow_value if main_net_inflow_value is not None else 0.0
    main_flow_percentile = number(quote.get("main_flow_percentile"))
    flow_snapshot = quote.get("flow_snapshot") if isinstance(quote.get("flow_snapshot"), dict) else {}
    bounded_watch_flow_only = str(flow_snapshot.get("scope") or "") == "explicit_watchlist_only"
    previous_price = number((previous_quote or {}).get("price"))
    # Cost is the explicit position marker.  Sellable quantity is optional
    # because A-share T+1 and partial fills can make it temporarily unknown;
    # we must not misclassify a held name as a fresh entry candidate merely
    # because the user has not entered a share count.
    holding = watch.get("entry_price") is not None
    signals: list[dict[str, Any]] = []
    observed_public_fields = [name for name, value in (
        ("volume_ratio", volume_ratio_value), ("turnover_rate", turnover_rate_value),
        ("main_net_inflow", main_net_inflow_value),
    ) if value is None]
    # Eastmoney supplies useful per-watch observations, but this selected
    # basket is neither a cross-section nor an independent capital-flow
    # universe. Keep it in evidence while treating it as unavailable for all
    # legacy flow/ranking rules.
    missing_public_fields = (
        ["volume_ratio", "turnover_rate", "main_net_inflow"]
        if bounded_watch_flow_only else observed_public_fields
    )
    if bounded_watch_flow_only:
        volume_ratio = turnover_rate = main_net_inflow = 0.0
    common = {"price": price, "pct_change": pct_change, "volume_ratio": volume_ratio_value,
              "turnover_rate": turnover_rate_value, "main_net_inflow": main_net_inflow_value,
              "main_flow_percentile": main_flow_percentile, "price_above_previous_scan": previous_price is None or price > previous_price,
              "data_availability": {"missing_public_flow_fields": missing_public_fields,
                                    "public_flow_available": not missing_public_fields,
                                    "eastmoney_watch_flow_observed_research_only": bounded_watch_flow_only},
              "daily_factors": daily_factors or {"status": "not_available"},
              "minute_features": minute_features or {"status": "not_available"},
              "peer_context": peer_context or {"status": "not_available"}}
    hard_stop = number(watch.get("hard_stop"))
    if holding and bool(watch.get("alert_on_exit")) and hard_stop is not None and price <= hard_stop:
        sellable = int(watch.get("available_quantity") or 0) > 0
        signals.append({"signal_key": f"{symbol}:exit:hard_stop", "signal_type": "exit", "severity": "critical",
                        "score": 100, "hard": True, "conditions": {**common, "hard_stop": hard_stop,
                                                                             "sellable_quantity_confirmed": sellable},
                        "risk_flags": ["hard_stop_triggered", "manual_review_required",
                                       *( [] if sellable else ["no_confirmed_sellable_quantity_risk_alert_only"])]})
    # A material opening gap can finish before six causal minute bars exist.
    # Record a research watch on a fresh direct quote, then leave every later
    # entry upgrade to the normal minute/flow confirmation rules.
    freshness = quote.get("price_freshness") if isinstance(quote.get("price_freshness"), dict) else {}
    opening_gap_watch = (
        opening_gap_window and not holding and bool(watch.get("alert_on_entry"))
        and 3.0 <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
        and str(quote.get("price_source") or "") == "tencent_batched_watch_quote"
        and str(freshness.get("status") or "") == "fresh"
    )
    if opening_gap_watch and not signals:
        signals.append({"signal_key": f"{symbol}:watch:opening_gap_continuation_v1", "signal_type": "watch",
                        "severity": "warning", "score": min(80, round(42 + pct_change * 6, 2)), "hard": False,
                        "alert_on_first_observation": True,
                        "conditions": {**common, "setup": "opening_gap_requires_minute_follow_through",
                                       "opening_gap_window": True, "minute_confirmation": "pending"},
                        "risk_flags": ["opening_gap_unconfirmed", "watch_only_not_entry", "manual_review_required",
                                       "no_automatic_order"]})
    strategy = (watch.get("metadata") or {}).get("surge_strategy") if isinstance(watch.get("metadata"), dict) else None
    minute_return_1m = number((minute_features or {}).get("return_1m_pct"))
    minute_return_3m = number((minute_features or {}).get("return_3m_pct"))
    minute_volume_multiple = number((minute_features or {}).get("minute_volume_multiple"))
    above_vwap_pct = number((minute_features or {}).get("above_vwap_pct"))
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    available_peers = int((peer_context or {}).get("available_peer_count") or 0)
    peer_breadth = float((peer_context or {}).get("confirming_breadth") or 0)
    upside_assessment = upside_assessment_fn(quote, daily_factors, minute_features, peer_context)
    common["upside_research_assessment"] = upside_assessment
    # Independent peers substitute for a second time sample in this opt-in
    # research rule.  The upper bounds deliberately reject late limit-up
    # chasing; this remains a manual candidate and is never executable.
    sector_surge = (
        isinstance(strategy, dict) and bool(strategy.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry"))
        and STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
        and minute_return_1m is not None and minute_return_1m >= 0.75
        and minute_return_3m is not None and 1.5 <= minute_return_3m <= 4.5
        and minute_volume_multiple is not None and minute_volume_multiple >= STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR
        and above_vwap_pct is not None and 0 <= above_vwap_pct <= 5.5
        and available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.66
    )
    if sector_surge and not signals:
        flow_bonus = 5 if main_flow_percentile is not None and main_flow_percentile >= 0.8 else 0
        signals.append({"signal_key": f"{symbol}:entry:sector_surge_v1", "signal_type": "entry", "severity": "warning",
                        "score": min(95, round(65 + min(minute_volume_multiple, 8) * 2 + peer_breadth * 10 + flow_bonus, 2)),
                        "hard": False, "independent_confirmation": True,
                        "conditions": {**common, "setup": "minute_price_volume_plus_sector_breadth",
                                       "sector_label": strategy.get("sector_label")},
                        "risk_flags": ["experimental_event_replay_rule", "independent_peer_confirmation",
                        "reject_above_6_5pct", "manual_review_required", "no_automatic_order"]})
    reversal_research = (watch.get("metadata") or {}).get("reversal_research") if isinstance(watch.get("metadata"), dict) else None
    previous_pct_change = number((previous_quote or {}).get("pct_change"))
    recovery_from_low = number((minute_features or {}).get("recovery_from_session_low_pct"))
    session_low_price = number((minute_features or {}).get("session_low_price"))
    implied_pre_close = price / (1 + pct_change / 100) if 1 + pct_change / 100 > 0 else None
    session_low_pct = ((session_low_price / implied_pre_close - 1) * 100
                       if session_low_price is not None and implied_pre_close and implied_pre_close > 0 else None)
    if previous_pct_change is None and previous_price is not None and implied_pre_close and implied_pre_close > 0:
        previous_pct_change = (previous_price / implied_pre_close - 1) * 100
    deep_reversal_confirmation = ((main_flow_percentile is not None and main_flow_percentile >= 0.9)
                                  or (available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.66))
    # A ground-to-sky session is split into two causal alerts.  The first is a
    # deep-reversal impulse watch while price may still be far below yesterday's
    # close.  Only a later reclaim of that close plus flow/peer confirmation is
    # a stage upgrade, and it remains a manual-research prompt.
    deep_reversal_impulse = (
        isinstance(reversal_research, dict) and bool(reversal_research.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry")) and session_low_pct is not None and session_low_pct <= -8.5
        and -8.0 <= pct_change <= 0.5 and recovery_from_low is not None and recovery_from_low >= 3.0
        and minute_return_3m is not None and minute_return_3m >= 1.2
        and minute_volume_multiple is not None and minute_volume_multiple >= 2.5
        and above_vwap_pct is not None and above_vwap_pct >= 0 and deep_reversal_confirmation
    )
    if deep_reversal_impulse and not signals:
        signals.append({"signal_key": f"{symbol}:watch:deep_reversal_impulse_v1", "signal_type": "watch", "severity": "warning",
                        "score": min(88, round(52 + min(minute_volume_multiple, 8) * 3 + min(recovery_from_low, 12) * 1.5, 2)),
                        "hard": False, "alert_on_first_observation": True,
                        "conditions": {**common, "setup": "deep_reversal_impulse_from_limit_down_zone",
                                       "session_low_pct": round(session_low_pct, 3),
                                       "recovery_from_session_low_pct": recovery_from_low,
                                       "research_label": reversal_research.get("label") or "ground_to_sky_research"},
                        "risk_flags": ["deep_reversal_extreme_volatility", "below_previous_close_impulse_only",
                                       "requires_previous_close_reclaim", "manual_review_required", "no_automatic_order"]})
    deep_reversal_acceptance = (
        isinstance(reversal_research, dict) and bool(reversal_research.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry")) and session_low_pct is not None and session_low_pct <= -8.5
        and 0 <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT and recovery_from_low is not None and recovery_from_low >= 9.0
        and minute_return_3m is not None and minute_return_3m >= 0.5
        and above_vwap_pct is not None and above_vwap_pct >= 0
        and deep_reversal_confirmation
        and (previous_pct_change is None or previous_pct_change <= 0.5 or price >= previous_price)
    )
    if deep_reversal_acceptance:
        signals.append({"signal_key": f"{symbol}:watch:deep_reversal_previous_close_acceptance_v1",
                        "signal_type": "watch", "severity": "warning",
                        "score": min(92, round(62 + min(recovery_from_low, 20) + max(0, minute_return_3m) * 2, 2)),
                        "hard": False, "independent_confirmation": True, "stage_upgrade": True,
                        "conditions": {**common, "setup": "deep_reversal_previous_close_reclaim",
                                       "session_low_pct": round(session_low_pct, 3),
                                       "previous_pct_change": previous_pct_change,
                                       "recovery_from_session_low_pct": recovery_from_low,
                                       "research_label": reversal_research.get("label") or "ground_to_sky_research"},
                        "risk_flags": ["deep_reversal_extreme_volatility", "previous_close_reclaimed",
                                       "requires_hold_confirmation", "manual_review_required", "no_automatic_order"]})
    # This is intentionally opt-in and emits a research watch, not an order.
    # It models the replayed 2026-08-10 green-to-red setups: reclaim after a
    # material session low, a short-window price burst, volume expansion,
    # VWAP recovery and either extreme same-source flow or peer breadth.
    green_reclaim_research = (
        isinstance(reversal_research, dict) and bool(reversal_research.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry")) and 0.5 <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
        and minute_return_3m is not None and 1.5 <= minute_return_3m <= 4.5
        and minute_volume_multiple is not None and minute_volume_multiple >= STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR
        and above_vwap_pct is not None and 0 <= above_vwap_pct <= 6.0
        and recovery_from_low is not None and recovery_from_low >= 3.0
        and ((previous_pct_change is not None and previous_pct_change <= 0 < pct_change)
             or recovery_from_low >= 4.0)
        and ((main_flow_percentile is not None and main_flow_percentile >= 0.9)
             or (available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.66))
    )
    if green_reclaim_research and not signals:
        confirmation = "flow_top_10pct" if main_flow_percentile is not None and main_flow_percentile >= 0.9 else "peer_breadth"
        signals.append({"signal_key": f"{symbol}:watch:green_reclaim_research_v1", "signal_type": "watch", "severity": "info",
                        "score": min(88, round(48 + min(minute_volume_multiple, 8) * 3 + min(recovery_from_low, 12) * 1.5, 2)),
                        "hard": False,
                        "conditions": {**common, "setup": "green_reclaim_price_volume_vwap",
                                       "previous_pct_change": previous_pct_change, "recovery_from_session_low_pct": recovery_from_low,
                                       "confirmation": confirmation,
                                       "research_label": reversal_research.get("label") or "green_reclaim"},
                        "risk_flags": ["experimental_research_mode", "requires_second_scan_confirmation",
                                       "manual_review_required", "no_automatic_order", "reject_above_6_5pct"]})
    upside_research = (watch.get("metadata") or {}).get("upside_research") if isinstance(watch.get("metadata"), dict) else None
    # This models a first high-of-day breakout rather than a green-to-red
    # recovery.  It is opt-in because a single strong session cannot establish
    # an alpha, and because the score is intended for replay diagnostics first.
    upside_breakout_research = (
        isinstance(upside_research, dict) and bool(upside_research.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry")) and upside_assessment.get("status") in {"candidate", "attention_only"}
    )
    if upside_breakout_research and not signals:
        attention_only = upside_assessment.get("status") == "attention_only"
        signals.append({"signal_key": f"{symbol}:watch:upside_breakout_eac_v3", "signal_type": "watch",
                        "severity": "warning" if attention_only else "info",
                        "score": upside_assessment["score"], "hard": False,
                        "alert_on_first_observation": True,
                        "upgrade_to_entry_on_repeat": not attention_only,
                        "conditions": {**common, "setup": "eac_first_intraday_high",
                                       "eac_state": upside_assessment["status"],
                                       "research_label": upside_research.get("label") or "upside_breakout"},
                        "risk_flags": ["experimental_research_mode", "requires_second_scan_confirmation",
                                       *upside_assessment.get("risk_flags", []),
                                       "manual_review_required", "no_automatic_order", "reject_above_6_5pct"]})
    leader_burst = (
        isinstance(strategy, dict) and bool(strategy.get("enabled")) and not holding
        and bool(watch.get("alert_on_entry")) and 0.5 <= pct_change <= 5.0
        and minute_return_1m is not None and minute_return_1m >= 0.8
        and minute_return_3m is not None and -0.5 <= minute_return_3m <= 3.0
        and minute_volume_multiple is not None and minute_volume_multiple >= 4.0
        and above_vwap_pct is not None and 0 <= above_vwap_pct <= 4.5
        and confirming_peers < 2
    )
    if leader_burst and not signals:
        signals.append({"signal_key": f"{symbol}:watch:leader_burst_v1", "signal_type": "watch", "severity": "info",
                        "score": min(85, round(55 + min(minute_volume_multiple, 8) * 2 + max(0, minute_return_1m) * 4, 2)),
                        "hard": False, "conditions": {**common, "setup": "leader_minute_burst",
                                                       "sector_label": strategy.get("sector_label")},
                        "risk_flags": ["leader_not_yet_confirmed_by_sector", "requires_second_scan_confirmation",
                                       "manual_review_required", "no_automatic_order"]})
    if holding and bool(watch.get("alert_on_exit")) and main_flow_percentile is not None and main_flow_percentile <= 0.01 and volume_ratio >= 1.5 and not signals:
        signals.append({"signal_key": f"{symbol}:reduce:extreme_flow_sell", "signal_type": "reduce", "severity": "warning",
                        "score": 85, "hard": False, "conditions": {**common, "flow_extreme": "bottom_1pct"},
                        "risk_flags": ["cross_sectional_extreme_sell", "requires_second_scan_confirmation", "manual_review_required"]})
    if main_flow_percentile is not None and main_flow_percentile >= 0.99 and volume_ratio >= 1.5 and not signals:
        signals.append({"signal_key": f"{symbol}:watch:extreme_flow_buy", "signal_type": "watch", "severity": "warning",
                        "score": 80, "hard": False, "conditions": {**common, "flow_extreme": "top_1pct"},
                        "risk_flags": ["cross_sectional_extreme_buy", "requires_second_scan_confirmation", "manual_review_required"]})
    # A late-session expansion is not an entry recommendation.  It is a
    # separate anomaly class: rapid day-level extension, unusually high
    # turnover and clearly positive cross-sectional flow can matter even when
    # the conventional volume-ratio field lags the move.
    if pct_change >= 6.0 and turnover_rate >= 12.0 and main_flow_percentile is not None and main_flow_percentile >= 0.95 and not signals:
        signals.append({"signal_key": f"{symbol}:watch:price_extension", "signal_type": "watch", "severity": "warning",
                        "score": min(90, round(45 + pct_change * 4 + turnover_rate, 2)), "hard": False,
                        "conditions": {**common, "price_extension": "pct_ge_6_turnover_ge_12_flow_top_5pct"},
                        "risk_flags": ["abnormal_price_extension", "requires_second_scan_confirmation", "not_an_entry_instruction", "manual_review_required"]})
    # Fuyao's all-A snapshot is deliberately price/volume/turnover-only: it
    # does not claim an exchange-grade main-flow field, and it does not expose
    # the legacy rolling volume-ratio/turnover pair.  Missing values must not
    # quietly become zero and freeze every entry forever.  When that public
    # flow contract is absent, replace it with independently captured minute
    # expansion plus exact point-in-time peer breadth.  This is still a
    # research candidate requiring a second scan, never an order instruction.
    legacy_public_entry_inputs_available = not missing_public_fields
    fuyao_minute_breadth_entry = (
        not legacy_public_entry_inputs_available and not holding
        and bool(watch.get("alert_on_entry"))
        and STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
        and previous_price is not None and price > previous_price
        and minute_return_1m is not None and minute_return_1m >= 0.75
        and minute_return_3m is not None and 1.5 <= minute_return_3m <= 4.5
        and minute_volume_multiple is not None and minute_volume_multiple >= STANDARD_MINUTE_VOLUME_MULTIPLE_FLOOR
        and above_vwap_pct is not None and 0 <= above_vwap_pct <= 5.5
        and available_peers >= 2 and confirming_peers >= 2 and peer_breadth >= 0.66
    )
    if fuyao_minute_breadth_entry and not signals:
        signals.append({"signal_key": f"{symbol}:entry:fuyao_minute_breadth_v1", "signal_type": "entry",
                        "severity": "info", "score": min(92, round(
                            52 + min(minute_volume_multiple, 8) * 3 + peer_breadth * 12, 2,
                        )), "hard": False, "independent_confirmation": True,
                        "conditions": {**common, "setup": "fuyao_minute_price_volume_plus_exact_peer_breadth",
                                       "flow_confirmation": ("eastmoney_watch_flow_observed_research_only"
                                                             if bounded_watch_flow_only else "not_required_fuyao_no_flow_semantics"),
                                       "price_confirmation": "direct_watch_quote_above_previous_scan"},
                        "risk_flags": ["fuyao_no_public_main_flow", "minute_volume_proxy",
                        "independent_peer_confirmation", "requires_second_scan_confirmation",
                                       *( ["eastmoney_watch_flow_research_confirmation_only"] if bounded_watch_flow_only else []),
                                       "manual_review_required", "no_automatic_order"]})
    entry_setup = (legacy_public_entry_inputs_available and not holding and bool(watch.get("alert_on_entry"))
                   and STANDARD_ENTRY_MIN_INTRADAY_GAIN_PCT <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
                   and volume_ratio >= 1.8 and turnover_rate >= 2.0
                   and main_net_inflow > 0 and previous_price is not None and price > previous_price)
    if entry_setup:
        signals.append({"signal_key": f"{symbol}:entry:{model_version}", "signal_type": "entry",
                        "severity": "info", "score": min(95, round(40 + volume_ratio * 10 + turnover_rate * 2, 2)), "hard": False,
                        "conditions": common, "risk_flags": ["requires_second_scan_confirmation", "manual_review_required"]})
    adverse = (holding and bool(watch.get("alert_on_exit")) and watch.get("entry_price") is not None
               and price <= float(watch["entry_price"]) * 0.97 and volume_ratio >= 1.5 and main_net_inflow < 0)
    if adverse and not signals:
        signals.append({"signal_key": f"{symbol}:reduce:{model_version}", "signal_type": "reduce",
                        "severity": "warning", "score": 70, "hard": False, "conditions": {**common, "entry_price": float(watch["entry_price"])},
                        "risk_flags": ["loss_with_negative_main_flow", "requires_second_scan_confirmation", "manual_review_required"]})
    volume_anomaly = volume_ratio >= 2.5 and turnover_rate >= 5.0
    if volume_anomaly and not signals:
        direction = "up" if pct_change > 0 else "down" if pct_change < 0 else "flat"
        signals.append({"signal_key": f"{symbol}:watch:volume_anomaly", "signal_type": "watch", "severity": "warning",
                        "score": min(90, round(30 + volume_ratio * 10 + turnover_rate * 2, 2)), "hard": False,
                        "conditions": {**common, "anomaly_direction": direction},
                        "risk_flags": ["abnormal_volume", "requires_second_scan_confirmation", "manual_review_required"]})
    return signals


__all__ = ["signal_rules"]
