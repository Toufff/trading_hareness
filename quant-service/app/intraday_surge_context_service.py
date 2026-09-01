"""Bounded Tencent minute-context capture for explicit intraday watches.

This service owns no provider client, database or process-global cache.  The
caller injects each boundary so the same narrow minute basket can be tested
without widening it into a public-market scan.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable


async def capture(
    watches: list[dict[str, Any]], *,
    mapped_peers: dict[str, dict[str, Any]] | None,
    priority_symbols: list[str] | None = None,
    cache: dict[str, tuple[float, dict[str, Any] | None, str | None]],
    max_symbols: Callable[[], int],
    open_capabilities: Callable[..., Awaitable[set[str]]],
    capability: str,
    fetch_minutes: Callable[[str], Awaitable[list[dict[str, Any]]]],
    minute_features: Callable[..., dict[str, Any]],
    persist_health: Callable[..., Any],
    run_database: Callable[..., Awaitable[Any]],
    safe_error: Callable[[str, int], str],
    handled_errors: tuple[type[BaseException], ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Capture a capped target/peer basket with a 45-second feature cache."""
    requested: list[str] = []
    mapped_peers = mapped_peers or {}
    configured_targets: list[str] = []
    configured_peers: list[str] = []
    passive_watches: list[str] = []
    mapped_peer_symbols: list[str] = []

    def append_unique(bucket: list[str], value: Any) -> None:
        symbol = str(value).upper()
        if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) and symbol not in bucket:
            bucket.append(symbol)

    # A quote-level anomaly is already evidence worth enriching.  Keep this
    # explicit, bounded list ahead of the normal target/peer/passive buckets
    # so a capped minute basket does not spend its budget on quiet names.
    priority: list[str] = []
    for value in priority_symbols or []:
        append_unique(priority, value)

    for watch in watches:
        watch_symbol = str(watch["symbol"]).upper()
        metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
        configurations = [
            metadata.get(key) for key in ("surge_strategy", "reversal_research", "upside_research")
            if isinstance(metadata.get(key), dict) and metadata[key].get("enabled")
        ]
        if configurations:
            append_unique(configured_targets, watch_symbol)
        else:
            append_unique(passive_watches, watch_symbol)
        for strategy in configurations:
            for value in strategy.get("peer_symbols") or []:
                append_unique(configured_peers, value)
        for value in (mapped_peers.get(watch_symbol) or {}).get("peer_symbols") or []:
            append_unique(mapped_peer_symbols, value)
    for bucket in (priority, configured_targets, configured_peers, passive_watches, mapped_peer_symbols):
        for symbol in bucket:
            if symbol not in requested:
                requested.append(symbol)

    requested_total = len(requested)
    requested = requested[:max_symbols()]
    now_monotonic = asyncio.get_running_loop().time()
    cache_ttl_seconds = 45.0
    for symbol, cached in list(cache.items()):
        if now_monotonic - cached[0] > cache_ttl_seconds * 4:
            cache.pop(symbol, None)
    cached_features: dict[str, dict[str, Any]] = {}
    cached_errors: dict[str, str] = {}
    missing: list[str] = []
    for symbol in requested:
        cached = cache.get(symbol)
        if cached is not None and now_monotonic - cached[0] <= cache_ttl_seconds:
            if cached[1] is not None:
                cached_features[symbol] = cached[1]
            elif cached[2]:
                cached_errors[symbol] = cached[2]
        else:
            missing.append(symbol)
    if missing and capability in await open_capabilities("tencent_free", [capability]):
        errors = {**cached_errors, **{symbol: "provider health circuit is open; upstream request skipped" for symbol in missing}}
        return cached_features, {
            "requested": requested, "requested_total": requested_total,
            "truncated": requested_total > len(requested), "completed": sorted(cached_features),
            "errors": errors, "cached_symbols": sorted(cached_features),
            "cache_ttl_seconds": cache_ttl_seconds, "provider_status": "circuit_open",
        }

    semaphore = asyncio.Semaphore(8)

    async def fetch_one(symbol: str) -> tuple[str, dict[str, Any] | None, str | None]:
        try:
            async with semaphore:
                rows = await asyncio.wait_for(fetch_minutes(symbol), timeout=6)
            return symbol, minute_features(rows, source="tencent_free_minute"), None
        except handled_errors as error:
            return symbol, None, str(error)[:240]

    started_at = asyncio.get_running_loop().time()
    tasks: dict[asyncio.Task[tuple[str, dict[str, Any] | None, str | None]], str] = {}
    pending: set[asyncio.Task[tuple[str, dict[str, Any] | None, str | None]]] = set()
    results: list[tuple[str, dict[str, Any] | None, str | None]] = []
    if missing:
        tasks = {asyncio.create_task(fetch_one(symbol)): symbol for symbol in missing}
        done, pending = await asyncio.wait(tasks, timeout=6.5)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            try:
                results.append(task.result())
            except Exception as error:  # noqa: BLE001 - one symbol never aborts its peers
                results.append((tasks[task], None, safe_error(str(error), 240)))
        results.extend((tasks[task], None, "minute_context_deadline_exceeded") for task in pending)

    features = dict(cached_features)
    errors = dict(cached_errors)
    fresh_errors: list[str] = []
    fresh_completed = 0
    for symbol, item, error in results:
        cache[symbol] = (now_monotonic, item, error)
        if item is not None:
            features[symbol] = item
            fresh_completed += 1
        elif error:
            errors[symbol] = error
            fresh_errors.append(error)
    if missing:
        await run_database(
            persist_health, fresh_completed, fresh_errors,
            round((asyncio.get_running_loop().time() - started_at) * 1000),
        )
    return features, {
        "requested": requested, "requested_total": requested_total,
        "truncated": requested_total > len(requested), "completed": sorted(features), "errors": errors,
        "cached_symbols": sorted(cached_features), "cache_ttl_seconds": cache_ttl_seconds,
        "priority": {
            "quote_anomaly_symbols": priority,
            "configured_targets": configured_targets, "configured_peers": configured_peers,
            "passive_watches": passive_watches, "mapped_peers": mapped_peer_symbols,
        },
        "deadline_exceeded_symbols": sorted(tasks[task] for task in pending),
        "provider_status": "completed" if fresh_completed else "failed" if fresh_errors else "cached",
    }


__all__ = ["capture"]
