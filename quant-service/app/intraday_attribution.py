"""Pure attribution labels for persisted intraday signal evidence."""

from __future__ import annotations

import re
from typing import Any, Callable


def signal_attribution(signal_key: str, signal_type: str, conditions: dict[str, Any] | None,
                       evidence: dict[str, Any] | None, market_context: dict[str, Any] | None = None,
                       *, number: Callable[[Any], float | None], signal_model_version: str) -> dict[str, Any]:
    conditions = conditions if isinstance(conditions, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    market_context = market_context if isinstance(market_context, dict) else evidence.get("market_context") if isinstance(evidence.get("market_context"), dict) else {}
    peer_context = evidence.get("peer_context") if isinstance(evidence.get("peer_context"), dict) else conditions.get("peer_context") if isinstance(conditions.get("peer_context"), dict) else {}
    confirming_peers, available_peers = int(peer_context.get("confirming_peer_count") or 0), int(peer_context.get("available_peer_count") or 0)
    board_matches = market_context.get("symbol_board_matches") if isinstance(market_context.get("symbol_board_matches"), list) else []
    positive_board = any((number(item.get("net_inflow")) or 0) > 0 for item in board_matches if isinstance(item, dict))
    nonpositive_board = bool(board_matches) and not positive_board
    if confirming_peers >= 2 and positive_board: sector_linkage = "peer_and_board_top10_confirmed"
    elif confirming_peers >= 2: sector_linkage = "peer_confirmed"
    elif positive_board: sector_linkage = "board_top10_positive"
    elif nonpositive_board: sector_linkage = "board_top10_nonpositive"
    elif available_peers >= 2: sector_linkage = "peers_not_confirmed"
    else: sector_linkage = "unobserved"
    setup = str(conditions.get("setup") or "")
    is_eac = (
        "upside_acceptance_eac_v4" in signal_key
        or "upside_breakout_eac_v3" in signal_key
        or setup in {"eac_acceptance_confirmed", "eac_first_intraday_high"}
    )
    if "countertrend_rebound" in signal_key or setup == "countertrend_rebound_confirmed_plus_intraday_acceptance":
        stage, model_version = "acceptance", "countertrend-rebound-v1"
    elif "upside_acceptance_eac_v4" in signal_key or setup == "eac_acceptance_confirmed":
        stage, model_version = "acceptance", "eac-v4"
    elif "upside_breakout_eac_v3" in signal_key or setup == "eac_first_intraday_high":
        stage, model_version = "expansion", "eac-v3"
    elif signal_type in {"reduce", "exit"}:
        stage, model_version = "risk_exit", signal_model_version
    elif "price_extension" in signal_key or "extreme_flow" in signal_key:
        stage, model_version = "extension_watch", "legacy-unversioned"
    else:
        match = re.search(r"watchlist-confirmation-v\d+", signal_key)
        stage, model_version = "generic", match.group(0) if match else "legacy-unversioned"
    assessment = (
        conditions.get("eac_acceptance_assessment")
        if is_eac and isinstance(conditions.get("eac_acceptance_assessment"), dict)
        else conditions.get("upside_research_assessment")
        if is_eac and isinstance(conditions.get("upside_research_assessment"), dict)
        else {}
    )
    risk_flags = conditions.get("risk_flags") if isinstance(conditions.get("risk_flags"), list) else []
    assessment_status = str(assessment.get("status") or "")
    volume_baseline = "ready" if assessment_status == "candidate" else "insufficient" if assessment_status == "attention_only" or "time_bucket_volume_baseline_insufficient" in risk_flags else "not_applicable"
    market_state = str(market_context.get("market_state") or "unknown")
    order_book = evidence.get("tencent_order_book") if isinstance(evidence.get("tencent_order_book"), dict) else {}
    microstructure = order_book.get("latest_features") if isinstance(order_book.get("latest_features"), dict) else order_book.get("features") if isinstance(order_book.get("features"), dict) else {}
    qi5 = number(microstructure.get("qi5"))
    ofi_label, ofi = next(((label, number(order_book.get(f"ofi_{label}"))) for label in ("30s", "1m", "5m") if int(order_book.get(f"ofi_{label}_sample_count") or 0) >= 3 and number(order_book.get(f"ofi_{label}")) is not None), (None, None))
    if microstructure.get("delta_status") == "ready" or order_book.get("status") == "observed":
        microstructure_state = "observed_bid_heavy" if qi5 is not None and qi5 >= 0.2 else "observed_ask_heavy" if qi5 is not None and qi5 <= -0.2 else "observed_balanced"
        if ofi is not None and ofi_label: microstructure_state += ("_positive_ofi" if ofi > 0 else "_negative_ofi" if ofi < 0 else "_flat_ofi") + f"_{ofi_label}"
        elif order_book.get("status") == "observed": microstructure_state += "_ofi_window_insufficient"
    elif microstructure.get("status") == "observed": microstructure_state = "first_snapshot_only"
    else: microstructure_state = "unobserved"
    minute = evidence.get("tencent_minute") if isinstance(evidence.get("tencent_minute"), dict) else {}
    corr, smart_q = number(minute.get("price_log_volume_corr_30m")), number(minute.get("smart_money_q_30m"))
    return {"attribution_version": "intraday-signal-attribution-v2", "model_version": model_version, "stage": stage,
            "market_state": market_state, "sector_linkage": sector_linkage, "volume_baseline": volume_baseline,
            "confirming_peer_count": confirming_peers, "available_peer_count": available_peers, "board_top10_match_count": len(board_matches),
            "board_snapshot_age_seconds": market_context.get("board_snapshot_age_seconds"), "microstructure_state": microstructure_state,
            "price_volume_state": "positive_corr" if corr is not None and corr >= 0.2 else "negative_corr" if corr is not None and corr <= -0.2 else "neutral_or_missing",
            "smart_money_state": "below_session_vwap" if smart_q is not None and smart_q < 0.995 else "above_session_vwap" if smart_q is not None and smart_q > 1.005 else "neutral_or_missing",
            "ofi_attribution_window": ofi_label,
            "microstructure_research_only": {key: order_book.get(key) for key in ("seal_erosion_ratio_5m", "seal_erosion_sample_count_5m", "kyle_lambda_proxy_5m", "kyle_lambda_proxy_sample_count_5m", "vpin_proxy_5m", "vpin_proxy_sample_count_5m", "cord_sign_alignment_5m", "cord_sample_count_5m")},
            "microstructure_notice": "CORD/VPIN/Kyle/seal erosion are uncalibrated evidence proxies; no live threshold effect"}


__all__ = ["signal_attribution"]
