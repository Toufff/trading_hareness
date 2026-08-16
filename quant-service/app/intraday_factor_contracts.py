"""Reviewed factor contracts for the intraday research graph.

The registry deliberately holds only factors that are already calculated from
recorded evidence.  Registering a factor makes its timing/quality assumptions
visible to the event ledger; it does not add a score to a live rule.
"""

from __future__ import annotations

from typing import Any, Iterable

from .strategy_contracts import FactorSpec, contract_payload


INTRADAY_FACTOR_CONTRACT_VERSION = "intraday-factor-contracts-v1"

FACTOR_SPECS: dict[str, FactorSpec] = {
    "minute_return_3m": FactorSpec(
        factor_key="minute_return_3m", version="v1", frequency="minute",
        inputs=("intraday_minute_sessions.close",), availability_clock="minute_close_received_at",
        minimum_history=4, quality_flags=("minute_feature_missing",),
        description="Three completed minute return; never uses the still-forming next minute.",
    ),
    "vwap_distance": FactorSpec(
        factor_key="vwap_distance", version="v1", frequency="minute",
        inputs=("intraday_minute_sessions.amount", "intraday_minute_sessions.volume", "intraday_minute_sessions.close"),
        availability_clock="minute_close_received_at", minimum_history=1,
        quality_flags=("minute_feature_missing",),
        description="Distance from session VWAP computed only from received session minutes.",
    ),
    "minute_volume_multiple": FactorSpec(
        factor_key="minute_volume_multiple", version="v1", frequency="minute",
        inputs=("intraday_minute_sessions.volume",), availability_clock="minute_close_received_at",
        minimum_history=6, quality_flags=("minute_feature_missing", "time_profile_insufficient"),
        description="Current completed-minute volume versus prior completed minutes; descriptive until replay validation.",
    ),
    "exact_watchlist_peer_breadth": FactorSpec(
        factor_key="exact_watchlist_peer_breadth", version="v1", frequency="minute",
        inputs=("sector_membership_history.taxonomy_key", "sector_membership_history.sector_key", "intraday_minute_sessions.close"),
        availability_clock="membership_available_at_and_minute_close_received_at", minimum_history=1,
        quality_flags=("membership_mapping_missing", "peer_feature_missing"),
        description="Only explicit watchlist symbols sharing the exact source taxonomy and sector key.",
    ),
    "public_flow_proxy": FactorSpec(
        factor_key="public_flow_proxy", version="v1", frequency="quote",
        inputs=("tencent_free.zljlr",), availability_clock="quote_observed_at", minimum_history=1,
        quality_flags=("flow_missing", "quote_stale"),
        description="Provider-labelled public main-flow proxy; never labelled exchange order-flow imbalance.",
    ),
    "order_book_proxy": FactorSpec(
        factor_key="order_book_proxy", version="v1", frequency="quote",
        inputs=("tencent_order_book.bid_ask_levels",), availability_clock="quote_observed_at", minimum_history=2,
        quality_flags=("order_book_missing", "one_sided_book"),
        live_use="attribution_only",
        description="Snapshot imbalance/erosion proxy, not true event-level OFI or VPIN.",
    ),
    "daily_rebound_state": FactorSpec(
        factor_key="daily_rebound_state", version="v1", frequency="daily",
        inputs=("canonical_bars_daily.research_close", "daily_market_summary"),
        availability_clock="daily_bar_available_at", minimum_history=60,
        quality_flags=("adj_factor_missing", "insufficient_history_60"),
        description="Prior completed-day countertrend state; no same-day daily close is consumed intraday.",
    ),
}


def factor_contracts(keys: Iterable[str]) -> list[dict[str, Any]]:
    """Serialize unique known contracts in a deterministic order."""
    payloads = []
    for key in sorted({str(key) for key in keys if str(key)}):
        spec = FACTOR_SPECS.get(key)
        if spec is not None:
            payloads.append(contract_payload(spec))
    return payloads


def contracts_for_signal(signal: dict[str, Any]) -> list[dict[str, Any]]:
    """Declare factors actually present in a signal's current evidence."""
    conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    keys: set[str] = {"public_flow_proxy"}
    minute = conditions.get("minute_features")
    if isinstance(minute, dict) and minute.get("status") != "not_available":
        keys.update({"minute_return_3m", "vwap_distance", "minute_volume_multiple"})
    peers = conditions.get("peer_context")
    if isinstance(peers, dict) and int(peers.get("requested_peer_count") or 0) > 0:
        keys.add("exact_watchlist_peer_breadth")
    order_book = conditions.get("order_book_proxy")
    # A snapshot proxy is evidence only.  Declare it whenever the scan has a
    # recorded observation, including a one-sided seal, so replay can explain
    # why it was observational rather than silently omitting that source.
    if isinstance(order_book, dict) and str(order_book.get("status") or "") == "observed":
        keys.add("order_book_proxy")
    if isinstance(conditions.get("daily_rebound_state"), dict):
        keys.add("daily_rebound_state")
    return factor_contracts(keys)


__all__ = ["FACTOR_SPECS", "INTRADAY_FACTOR_CONTRACT_VERSION", "contracts_for_signal", "factor_contracts"]
