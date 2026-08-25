"""Prepare all bounded local inputs for one intraday scan transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
import uuid

from psycopg.types.json import Json


@dataclass(frozen=True)
class IntradayScanPreparedInputs:
    local_trade_date: date
    session_start: datetime
    order_book_by_symbol: dict[str, list[dict[str, Any]]]
    paper_positions: dict[str, dict[str, Any]]
    candidate_sector_keys: dict[str, list[str]]
    snapshot_payload: dict[str, Any]
    market_contexts: dict[tuple[datetime, str], dict[str, Any]]
    shadow_priors: dict[str, dict[str, Any]]
    rebound_priors: dict[str, dict[str, Any]]
    probability_profiles: dict[str, Any]
    daily_factors_by_symbol: dict[str, dict[str, Any]]
    raw_minute_features_by_symbol: dict[str, dict[str, Any] | None]
    minute_volume_profiles_by_symbol: dict[str, dict[str, Any]]
    previous_by_symbol: dict[str, dict[str, Any]]
    first_eac_by_symbol: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class IntradayScanPreparationDependencies:
    roll_positions_sellable: Callable[..., Any]
    record_provider_success: Callable[..., Any]
    record_provider_failure: Callable[..., Any]
    json_safe: Callable[[Any], Any]
    persist_portfolio_snapshot: Callable[..., Any]
    load_local_state: Callable[..., Any]
    clear_stale_episodes: Callable[..., Any]
    market_context_batch: Callable[..., dict[tuple[datetime, str], dict[str, Any]]]
    shadow_priors: Callable[[Any], dict[str, dict[str, Any]]]
    rebound_priors: Callable[[Any], dict[str, dict[str, Any]]]
    probability_profiles: Callable[[Any], dict[str, Any]]
    daily_factors: Callable[..., dict[str, dict[str, Any]]]
    minute_volume_profiles: Callable[..., dict[str, dict[str, Any]]]
    quote_source: Callable[[dict[str, Any] | None], str]
    previous_quote_frames: Callable[..., dict[str, dict[str, Any]]]
    first_eac_events: Callable[..., dict[str, dict[str, Any]]]
    minute_bucket: Callable[..., Any]
    number: Callable[[Any], float | None]


def prepare_intraday_scan_inputs(
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
    confirmation_window: timedelta,
    dependencies: IntradayScanPreparationDependencies,
) -> IntradayScanPreparedInputs:
    """Record scan-level facts and load all pre-signal local state.

    The caller owns the surrounding transaction.  No provider request, clock
    mutation or delivery occurs in this function.
    """
    local_trade_date = observed_at.astimezone(timezone(timedelta(hours=8))).date()
    dependencies.roll_positions_sellable(connection, trading_date=local_trade_date)
    if all_a_rows:
        dependencies.record_provider_success(connection, "fuyao_ths", "realtime_quote", len(all_a_rows), quote_latency_ms)
    else:
        dependencies.record_provider_failure(
            connection, "fuyao_ths", "realtime_quote", "all-A Fuyao snapshot unavailable during watch scan", quote_latency_ms,
        )
    connection.execute(
        """INSERT INTO quant.intraday_scan_runs(scan_id,observed_at,status,requested_symbols,source_status,summary)
           VALUES(%s,%s,'completed',%s,%s,%s)""",
        (scan_id, observed_at, Json(selected_symbols), Json(dependencies.json_safe(source_status)), Json({"watched": len(watches)})),
    )
    account = connection.execute("SELECT cash FROM quant.paper_accounts WHERE account_key='default'").fetchone()
    prior_snapshot = connection.execute(
        "SELECT equity,payload FROM quant.paper_portfolio_snapshots ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    prior_payload = dict(prior_snapshot["payload"] or {}) if prior_snapshot else {}
    dependencies.persist_portfolio_snapshot(
        connection, as_of=observed_at, quotes=quotes,
        cash=float(account["cash"]) if account is not None else 0,
        previous_equity=float(prior_snapshot["equity"]) if prior_snapshot and prior_snapshot["equity"] is not None else None,
        previous_close_equity=float(prior_payload.get("previous_close_equity") or 0) or None,
    )
    session_start = observed_at.astimezone(timezone(timedelta(hours=8))).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).astimezone(timezone.utc)
    local_state = dependencies.load_local_state(
        connection, selected_symbols, observed_at=observed_at, session_start=session_start, local_trade_date=local_trade_date,
    )
    dependencies.clear_stale_episodes(connection, selected_symbols, observed_at)
    market_contexts = dependencies.market_context_batch(connection, [(observed_at, symbol) for symbol in selected_symbols])
    raw_minute_features_by_symbol = {
        symbol: (tushare_minutes.get(symbol) or {}).get("feature") or surge_features.get(symbol)
        for symbol in selected_symbols
    }
    minute_volume_profiles_by_symbol = dependencies.minute_volume_profiles(
        {symbol: (feature or {}).get("time") for symbol, feature in raw_minute_features_by_symbol.items() if feature is not None},
        local_trade_date, connection, minute_bucket_fn=dependencies.minute_bucket, number=dependencies.number,
    )
    quote_sources = {str(watch["symbol"]): dependencies.quote_source(quotes.get(str(watch["symbol"]))) for watch in watches}
    return IntradayScanPreparedInputs(
        local_trade_date=local_trade_date, session_start=session_start,
        order_book_by_symbol=local_state.order_book_by_symbol, paper_positions=local_state.paper_positions,
        candidate_sector_keys=local_state.candidate_sector_keys, snapshot_payload=local_state.snapshot_payload,
        market_contexts=market_contexts, shadow_priors=dependencies.shadow_priors(connection),
        rebound_priors=dependencies.rebound_priors(connection), probability_profiles=dependencies.probability_profiles(connection),
        daily_factors_by_symbol=dependencies.daily_factors(selected_symbols, connection, number=dependencies.number),
        raw_minute_features_by_symbol=raw_minute_features_by_symbol,
        minute_volume_profiles_by_symbol=minute_volume_profiles_by_symbol,
        previous_by_symbol=dependencies.previous_quote_frames(
            connection, quote_sources, not_before=max(session_start, observed_at - timedelta(seconds=15)), observed_at=observed_at,
        ),
        first_eac_by_symbol=dependencies.first_eac_events(
            connection, selected_symbols, not_before=observed_at - confirmation_window,
        ),
    )


__all__ = [
    "IntradayScanPreparedInputs", "IntradayScanPreparationDependencies", "prepare_intraday_scan_inputs",
]
