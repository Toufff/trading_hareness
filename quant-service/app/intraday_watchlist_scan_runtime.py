"""Application adapter for one bounded intraday watchlist scan.

The service module owns scan sequencing and research-only alert semantics.
This adapter owns database/external-resource callbacks, allowing the ASGI root
to compose ports without embedding transactional closures in a route helper.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from .intraday_watch_quote_capture import WatchQuoteCaptureDependencies, capture_watch_quotes
from .intraday_watchlist_scan_service import IntradayWatchlistScanDependencies
from .ten_day_leader_rotation_intraday_service import TenDayLeaderRotationIntradayDependencies


@dataclass(frozen=True)
class IntradayWatchlistScanRuntimeDependencies:
    clock: Callable[[], float]
    observe_duration: Callable[[str, float], None]
    now_utc: Callable[[], datetime]
    new_scan_id: Callable[[], uuid.UUID]
    async_database: Any
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    watchlist_capacity: Callable[[int], dict[str, Any]]
    read_watchlists: Callable[..., Awaitable[list[dict[str, Any]]]]
    persist_terminal: Callable[..., Any]
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]]
    prune_rule_inputs: Callable[[datetime], Awaitable[None]]
    retry_pending_alerts: Callable[[], Awaitable[dict[str, int]]]
    read_exact_memberships: Callable[..., Awaitable[list[dict[str, Any]]]]
    mapped_peers: Callable[[list[str], list[dict[str, Any]]], dict[str, dict[str, Any]]]
    high_frequency_window: Callable[[datetime], bool]
    quote_capture_dependencies: WatchQuoteCaptureDependencies
    surge_context: Callable[..., Awaitable[tuple[dict[str, dict[str, Any]], dict[str, Any]]]]
    peer_context: Callable[[list[str], dict[str, dict[str, Any]]], dict[str, Any]]
    watch_priority_key: Callable[[dict[str, Any]], Any]
    realtime_validation_slice: Callable[[list[str], int, int], tuple[list[str], int]]
    tushare_minutes: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
    fast_confirmations: Callable[[list[str], dict[str, dict[str, Any]], datetime], Awaitable[dict[str, dict[str, Any]]]]
    board_cache_evidence: Callable[[datetime], Awaitable[dict[str, Any]]]
    build_source_status: Callable[..., dict[str, Any]]
    persist_signals: Callable[..., Any]
    read_shadow_pool: Callable[..., Awaitable[dict[str, Any]]]
    shadow_rotation_due: Callable[[datetime], bool]
    shadow_rotation_slice: Callable[[list[dict[str, Any]], datetime], tuple[list[dict[str, Any]], int]]
    tencent_watch_quotes: Callable[..., Awaitable[list[dict[str, Any]]]]
    merge_watch_prices: Callable[[dict[str, dict[str, Any]], list[dict[str, Any]]], Any]
    safe_error: Callable[[str, int], str]
    shadow_quote_errors: tuple[type[Exception], ...]
    rotation_persistence_dependencies: TenDayLeaderRotationIntradayDependencies
    persist_rotation_observations: Callable[..., dict[str, Any]]
    persist_rotation_scan_status: Callable[..., Any]
    json_safe: Callable[[Any], Any]
    deliver_alert: Callable[[uuid.UUID, str], Awaitable[dict[str, Any]]]
    alert_text: Callable[..., str]
    decision_card_url: Callable[[str], str | None]
    run_scan: Callable[..., Awaitable[dict[str, Any]]]
    xiaojie_leader_flow: Callable[..., Awaitable[dict[str, Any]]] | None = None


class IntradayWatchlistScanRuntime:
    """Build the scan-service ports and retain its bounded I/O semantics."""

    def __init__(self, dependencies: IntradayWatchlistScanRuntimeDependencies) -> None:
        self._dependencies = dependencies

    def _scan_dependencies(self) -> IntradayWatchlistScanDependencies:
        dependencies = self._dependencies

        async def load_watches(requested_symbols: list[str]) -> list[dict[str, Any]]:
            capacity = int(dependencies.watchlist_capacity(0)["max_symbols"])
            return await dependencies.read_watchlists(
                dependencies.async_database, requested_symbols, max_symbols=capacity,
            )

        async def persist_terminal(
            scan_id: uuid.UUID, observed_at: datetime, status: str, requested_symbols: list[str],
            source_status: dict[str, Any], summary: dict[str, Any],
        ) -> None:
            await dependencies.run_database(
                dependencies.persist_terminal, dependencies.database, scan_id, observed_at, status,
                requested_symbols, source_status, summary,
            )

        async def memberships(symbols: list[str], observed_at: datetime) -> list[dict[str, Any]]:
            return await dependencies.read_exact_memberships(dependencies.async_database, symbols, observed_at)

        async def capture_quotes(
            symbols: list[str], observed_at: datetime, quote_timestamp_slo_seconds: float,
        ) -> Any:
            return await capture_watch_quotes(
                symbols, observed_at, quote_timestamp_slo_seconds,
                dependencies.quote_capture_dependencies,
            )

        async def persist_signals(*args: Any) -> list[dict[str, Any]]:
            return await dependencies.run_database(
                dependencies.persist_signals, *args, timeout_seconds=60,
            )

        async def shadow_pool() -> dict[str, Any]:
            return await dependencies.read_shadow_pool(dependencies.async_database)

        async def capture_shadow_quotes(
            symbols: list[str],
        ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
            try:
                rows = await dependencies.tencent_watch_quotes(symbols, max_symbols=40)
            except dependencies.shadow_quote_errors as error:
                return {}, {
                    "status": "unavailable",
                    "error": dependencies.safe_error(str(error), 240),
                    "requested": len(symbols),
                }
            quotes: dict[str, dict[str, Any]] = {}
            dependencies.merge_watch_prices(quotes, rows)
            return quotes, {
                "status": "completed", "requested": len(symbols), "matched": len(quotes),
                "source": "tencent_batched_watch_quote",
            }

        async def persist_shadow_observations(**kwargs: Any) -> dict[str, Any]:
            return await dependencies.run_database(
                lambda: dependencies.persist_rotation_observations(
                    dependencies=dependencies.rotation_persistence_dependencies,
                    **kwargs,
                ),
                timeout_seconds=30,
            )

        async def persist_shadow_status(scan_id: uuid.UUID, status: dict[str, Any]) -> None:
            def write() -> None:
                with dependencies.database.transaction() as connection:
                    dependencies.persist_rotation_scan_status(
                        connection, scan_id=scan_id, status=status, json_safe=dependencies.json_safe,
                    )
            await dependencies.run_database(write, timeout_seconds=10)

        return IntradayWatchlistScanDependencies(
            now_utc=dependencies.now_utc,
            new_scan_id=dependencies.new_scan_id,
            realtime_session=dependencies.realtime_session,
            load_watches=load_watches,
            watchlist_capacity=dependencies.watchlist_capacity,
            persist_terminal=persist_terminal,
            prune_rule_inputs=dependencies.prune_rule_inputs,
            retry_pending_alerts=dependencies.retry_pending_alerts,
            load_exact_memberships=memberships,
            mapped_peers=dependencies.mapped_peers,
            high_frequency_window=dependencies.high_frequency_window,
            capture_quotes=capture_quotes,
            surge_context=dependencies.surge_context,
            peer_context=dependencies.peer_context,
            watch_priority_key=dependencies.watch_priority_key,
            realtime_validation_slice=dependencies.realtime_validation_slice,
            tushare_minutes=dependencies.tushare_minutes,
            fast_confirmations=dependencies.fast_confirmations,
            board_cache_evidence=dependencies.board_cache_evidence,
            build_source_status=dependencies.build_source_status,
            persist_signals=persist_signals,
            shadow_pool=shadow_pool,
            shadow_rotation_due=dependencies.shadow_rotation_due,
            shadow_rotation_slice=dependencies.shadow_rotation_slice,
            capture_shadow_quotes=capture_shadow_quotes,
            persist_shadow_observations=persist_shadow_observations,
            persist_shadow_status=persist_shadow_status,
            xiaojie_leader_flow=dependencies.xiaojie_leader_flow,
            deliver_alert=dependencies.deliver_alert,
            alert_text=dependencies.alert_text,
            decision_card_url=dependencies.decision_card_url,
        )

    async def run(self, request: Any) -> dict[str, Any]:
        started_at = self._dependencies.clock()
        payload = await self._dependencies.run_scan(request, self._scan_dependencies())
        self._dependencies.observe_duration(
            str(payload.get("status") or "unknown"),
            max(0.0, self._dependencies.clock() - started_at),
        )
        return payload


__all__ = [
    "IntradayWatchlistScanRuntime",
    "IntradayWatchlistScanRuntimeDependencies",
]
