"""One-transaction orchestration for persisting intraday scan evidence.

The caller owns the database transaction.  This module deliberately has no
database, provider, clock, or alert-delivery ownership: it freezes the inputs
already gathered by the scan, emits candidates, and delegates their durable
event state to injected collaborators.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
import uuid

from psycopg.types.json import Json


@dataclass(frozen=True)
class IntradayScanSignalPersistenceDependencies:
    prepare_inputs: Callable[..., Any]
    preparation_dependencies: Any
    quote_source: Callable[[dict[str, Any] | None], str]
    json_safe: Callable[[Any], Any]
    persist_rule_input_snapshot: Callable[..., Any]
    attach_volume_time_profile: Callable[..., dict[str, Any] | None]
    number: Callable[[Any], float | None]
    aggregate_order_book_observations: Callable[..., dict[str, Any] | None]
    generate_signals: Callable[..., list[dict[str, Any]]]
    signal_generation_dependencies: Any
    load_event_state: Callable[..., Any]
    persist_generated_signals: Callable[..., list[dict[str, Any]]]
    signal_event_persistence_dependencies: Any


@dataclass(frozen=True)
class IntradayScanPersistenceServiceDependencies:
    """Application-owned collaborators for the scan persistence boundary.

    ``persist_scan_signals`` deliberately receives an already-open connection
    so it remains straightforward to unit-test.  The live service, however,
    must never accidentally split its scan receipt, point-in-time inputs and
    event de-duplication across transactions.  Keep that transaction boundary
    in this small adapter rather than re-implementing it in the FastAPI
    composition module.
    """

    database: Any
    signal_dependencies: IntradayScanSignalPersistenceDependencies
    confirmation_window: Any
    signal_model_version: str
    factor_contract_version: str


def persist_scan_transaction(
    dependencies: IntradayScanPersistenceServiceDependencies,
    *,
    scan_id: uuid.UUID,
    observed_at: datetime,
    selected_symbols: list[str],
    source_status: dict[str, Any],
    watches: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    all_a_rows: list[dict[str, Any]],
    quote_latency_ms: int,
    tushare_minutes: dict[str, dict[str, Any]],
    surge_features: dict[str, dict[str, Any]],
    peer_contexts: dict[str, dict[str, Any]],
    fast_confirmations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Persist a live scan under its one required local transaction.

    This function performs no provider, clock or delivery I/O.  It only owns
    the durable transaction boundary used by the bounded database executor.
    """
    with dependencies.database.transaction() as connection:
        return persist_scan_signals(
            connection,
            scan_id=scan_id,
            observed_at=observed_at,
            selected_symbols=selected_symbols,
            source_status=source_status,
            watches=watches,
            quotes=quotes,
            all_a_rows=all_a_rows,
            quote_latency_ms=quote_latency_ms,
            tushare_minutes=tushare_minutes,
            surge_features=surge_features,
            peer_contexts=peer_contexts,
            fast_confirmations=fast_confirmations,
            confirmation_window=dependencies.confirmation_window,
            signal_model_version=dependencies.signal_model_version,
            factor_contract_version=dependencies.factor_contract_version,
            dependencies=dependencies.signal_dependencies,
        )


def persist_scan_signals(
    connection: Any,
    *,
    scan_id: uuid.UUID,
    observed_at: datetime,
    selected_symbols: list[str],
    source_status: dict[str, Any],
    watches: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    all_a_rows: list[dict[str, Any]],
    quote_latency_ms: int,
    tushare_minutes: dict[str, dict[str, Any]],
    surge_features: dict[str, dict[str, Any]],
    peer_contexts: dict[str, dict[str, Any]],
    fast_confirmations: dict[str, dict[str, Any]],
    confirmation_window: Any,
    signal_model_version: str,
    factor_contract_version: str,
    dependencies: IntradayScanSignalPersistenceDependencies,
) -> list[dict[str, Any]]:
    """Persist one scan using the caller's already-open transaction."""
    prepared = dependencies.prepare_inputs(
        connection, scan_id=scan_id, observed_at=observed_at, selected_symbols=selected_symbols,
        source_status=source_status, watches=watches, quotes=quotes, all_a_rows=all_a_rows,
        quote_latency_ms=quote_latency_ms, tushare_minutes=tushare_minutes, surge_features=surge_features,
        confirmation_window=confirmation_window, dependencies=dependencies.preparation_dependencies,
    )
    signals: list[dict[str, Any]] = []
    for watch in watches:
        symbol = str(watch["symbol"])
        quote = quotes.get(symbol)
        quote_source_name = dependencies.quote_source(quote)
        previous = prepared.previous_by_symbol.get(symbol)
        if quote:
            quote_raw = dict(quote.get("raw") or {})
            quote_raw["_observation_source"] = quote_source_name
            quote_raw["_price_source"] = quote.get("price_source")
            connection.execute(
                """INSERT INTO quant.intraday_quote_observations(scan_id,symbol,observed_at,source_name,price,pct_change,volume_ratio,turnover_rate,main_net_inflow,raw)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    scan_id, symbol, observed_at, quote_source_name, quote.get("price"), quote.get("pct_change"),
                    quote.get("volume_ratio"), quote.get("turnover_rate"), quote.get("main_net_inflow"),
                    Json(dependencies.json_safe(quote_raw)),
                ),
            )
        daily_factors = prepared.daily_factors_by_symbol.get(symbol, {"status": "insufficient_history", "bar_count": 0})
        minute_feature = dependencies.attach_volume_time_profile(
            prepared.raw_minute_features_by_symbol.get(symbol), prepared.minute_volume_profiles_by_symbol.get(symbol),
            number=dependencies.number,
        )
        order_book_feature = dependencies.aggregate_order_book_observations(
            prepared.order_book_by_symbol.get(symbol, []), observed_at,
        )
        peer_context = peer_contexts.get(symbol)
        previous_quote = dict(previous) if previous else None
        fast_confirmation = fast_confirmations.get(symbol, {"status": "missing", "max_age_seconds": 30})
        market_context = prepared.market_contexts.get((observed_at, symbol), {})
        portfolio_context = {
            "position": prepared.paper_positions.get(symbol) or {},
            "snapshot": prepared.snapshot_payload,
            "candidate_sector_keys": prepared.candidate_sector_keys.get(symbol, ()),
        }
        dependencies.persist_rule_input_snapshot(
            connection, scan_id=scan_id, observed_at=observed_at, watch=watch, quote=quote,
            previous_quote=previous_quote, daily_factors=daily_factors, minute_features=minute_feature,
            peer_context=peer_context, model_version=signal_model_version,
            market_context=market_context, fast_confirmation=fast_confirmation,
            portfolio_context=portfolio_context,
        )
        generated_signals = dependencies.generate_signals(
            watch=watch, symbol=symbol, quote=quote, previous_quote=previous_quote,
            daily_factors=daily_factors, minute_features=minute_feature, peer_context=peer_context,
            shadow_prior=prepared.shadow_priors.get(symbol), rebound_prior=prepared.rebound_priors.get(symbol),
            first_eac=prepared.first_eac_by_symbol.get(symbol), observed_at=observed_at,
            dependencies=dependencies.signal_generation_dependencies,
        )
        event_state = dependencies.load_event_state(
            connection, [str(signal["signal_key"]) for signal in generated_signals], symbol,
            session_start=prepared.session_start,
        )
        signals.extend(dependencies.persist_generated_signals(
            connection, scan_id=scan_id, observed_at=observed_at, symbol=symbol, watch=watch,
            quote=quote, daily_factors=daily_factors, minute_feature=minute_feature,
            peer_context=peer_context, market_context=market_context, fast_confirmation=fast_confirmation,
            order_book_feature=order_book_feature, tushare_minute=tushare_minutes.get(symbol),
            paper_position=prepared.paper_positions.get(symbol), portfolio_snapshot=prepared.snapshot_payload,
            candidate_sector_keys=prepared.candidate_sector_keys.get(symbol, ()), probability_profiles=prepared.probability_profiles,
            generated_signals=generated_signals, existing_event_state=event_state,
            confirmation_window=confirmation_window, factor_contract_version=factor_contract_version,
            dependencies=dependencies.signal_event_persistence_dependencies,
        ))
    return signals


__all__ = [
    "IntradayScanPersistenceServiceDependencies",
    "IntradayScanSignalPersistenceDependencies",
    "persist_scan_signals",
    "persist_scan_transaction",
]
