"""Provider-agnostic orchestration for one bounded intraday watchlist scan."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
import uuid


def quote_volume_anomaly_symbols(
    watches: list[dict[str, Any]], quotes: dict[str, dict[str, Any]],
) -> list[str]:
    """Rank quote-level volume anomalies for immediate minute enrichment.

    This deliberately mirrors the live volume-anomaly floors (2.5x volume
    ratio and 5% turnover), including per-symbol metadata floors.  It only
    changes evidence collection priority; it never creates or upgrades a
    signal by itself.
    """
    candidates: list[tuple[float, str]] = []
    for watch in watches:
        symbol = str(watch.get("symbol") or "").upper()
        quote = quotes.get(symbol) or {}
        try:
            volume_ratio = float(quote.get("volume_ratio"))
            turnover_rate = float(quote.get("turnover_rate"))
        except (TypeError, ValueError):
            continue
        metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
        thresholds = metadata.get("volume_anomaly_thresholds")
        thresholds = thresholds if isinstance(thresholds, dict) else {}
        try:
            ratio_gate = max(2.5, float(thresholds.get("volume_ratio_p95") or 0))
        except (TypeError, ValueError):
            ratio_gate = 2.5
        try:
            turnover_gate = max(5.0, float(thresholds.get("turnover_rate_p90") or 0))
        except (TypeError, ValueError):
            turnover_gate = 5.0
        if volume_ratio < ratio_gate or turnover_rate < turnover_gate:
            continue
        # A simple excess score keeps the ordering deterministic and gives
        # the most unusual names the first slots in the bounded basket.
        score = (volume_ratio / ratio_gate) + (turnover_rate / turnover_gate)
        candidates.append((score, symbol))
    return [symbol for _score, symbol in sorted(candidates, key=lambda item: (-item[0], item[1]))]


def inject_anomaly_rotation_priority(
    selected_symbols: list[str], anomaly_symbols: list[str], limit: int,
) -> list[str]:
    """Ensure the bounded Tushare validation slice includes top anomalies."""
    if limit <= 0:
        return []
    ordered: list[str] = []
    for symbol in anomaly_symbols + selected_symbols:
        symbol = str(symbol).upper()
        if symbol and symbol not in ordered:
            ordered.append(symbol)
    return ordered[:limit]


async def _surge_context_with_priority(
    surge_context: Callable[..., Awaitable[tuple[dict[str, dict[str, Any]], dict[str, Any]]]],
    watches: list[dict[str, Any]], mapped_peers: dict[str, dict[str, Any]],
    priority_symbols: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Call old injected test adapters while supporting priority-aware ones."""
    try:
        return await surge_context(watches, mapped_peers=mapped_peers, priority_symbols=priority_symbols)
    except TypeError as error:
        # Third-party/replay adapters written before priority_symbols existed
        # remain usable.  Do not hide a TypeError raised by the adapter body.
        if "priority_symbols" not in str(error):
            raise
        return await surge_context(watches, mapped_peers=mapped_peers)


@dataclass(frozen=True)
class IntradayWatchlistScanDependencies:
    now_utc: Callable[[], datetime]
    new_scan_id: Callable[[], uuid.UUID]
    realtime_session: Callable[[], Awaitable[tuple[bool, str]]]
    load_watches: Callable[[list[str]], Awaitable[list[dict[str, Any]]]]
    watchlist_capacity: Callable[[int], dict[str, Any]]
    persist_terminal: Callable[..., Awaitable[None]]
    prune_rule_inputs: Callable[[datetime], Awaitable[None]]
    retry_pending_alerts: Callable[[], Awaitable[dict[str, int]]]
    load_exact_memberships: Callable[[list[str], datetime], Awaitable[list[dict[str, Any]]]]
    mapped_peers: Callable[[list[str], list[dict[str, Any]]], dict[str, dict[str, Any]]]
    high_frequency_window: Callable[[datetime], bool]
    capture_quotes: Callable[[list[str], datetime, float], Awaitable[Any]]
    surge_context: Callable[..., Awaitable[tuple[dict[str, dict[str, Any]], dict[str, Any]]]]
    peer_context: Callable[[list[str], dict[str, dict[str, Any]]], dict[str, Any]]
    watch_priority_key: Callable[[dict[str, Any]], Any]
    realtime_validation_slice: Callable[[list[str], int, int], tuple[list[str], int]]
    tushare_minutes: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
    fast_confirmations: Callable[[list[str], dict[str, dict[str, Any]], datetime], Awaitable[dict[str, dict[str, Any]]]]
    board_cache_evidence: Callable[[datetime], Awaitable[dict[str, Any]]]
    build_source_status: Callable[..., dict[str, Any]]
    persist_signals: Callable[..., Awaitable[list[dict[str, Any]]]]
    shadow_pool: Callable[[], Awaitable[dict[str, Any]]]
    shadow_rotation_due: Callable[[datetime], bool]
    shadow_rotation_slice: Callable[[list[dict[str, Any]], datetime], tuple[list[dict[str, Any]], int]]
    capture_shadow_quotes: Callable[[list[str]], Awaitable[tuple[dict[str, dict[str, Any]], dict[str, Any]]]]
    persist_shadow_observations: Callable[..., Awaitable[dict[str, Any]]]
    persist_shadow_status: Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]]
    deliver_alert: Callable[[uuid.UUID, str], Awaitable[dict[str, Any]]]
    alert_text: Callable[..., str]
    decision_card_url: Callable[[str], str | None]
    xiaojie_leader_flow: Callable[..., Awaitable[dict[str, Any]]] | None = None
    persist_xiaojie_status: Callable[[uuid.UUID, dict[str, Any]], Awaitable[None]] | None = None


def build_peer_contexts(
    watches: list[dict[str, Any]],
    mapped_peer_groups: dict[str, dict[str, Any]],
    surge_features: dict[str, dict[str, Any]],
    peer_context: Callable[[list[str], dict[str, dict[str, Any]]], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Join explicit configured peers with exact point-in-time memberships."""
    contexts: dict[str, dict[str, Any]] = {}
    for watch in watches:
        symbol = str(watch["symbol"]).upper()
        metadata = watch.get("metadata") if isinstance(watch.get("metadata"), dict) else {}
        configurations = [
            metadata.get(key)
            for key in ("surge_strategy", "reversal_research", "upside_research")
            if isinstance(metadata.get(key), dict) and metadata[key].get("enabled")
        ]
        configured_peers = [
            str(value).upper()
            for strategy in configurations
            for value in strategy.get("peer_symbols") or []
            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(value).upper()) and str(value).upper() != symbol
        ]
        mapped = mapped_peer_groups.get(symbol) or {"peer_symbols": [], "groups": []}
        peers = sorted(set(configured_peers) | set(mapped.get("peer_symbols") or []))
        contexts[symbol] = {
            **peer_context(peers, surge_features),
            "configured_peer_symbols": sorted(set(configured_peers)),
            "mapped_peer_symbols": list(mapped.get("peer_symbols") or []),
            "exact_membership_groups": list(mapped.get("groups") or []),
        }
    return contexts


async def run_watchlist_scan(request: Any, dependencies: IntradayWatchlistScanDependencies) -> dict[str, Any]:
    """Run one scan without owning database transactions or provider clients."""
    observed_at = dependencies.now_utc()
    active, reason = await dependencies.realtime_session()
    watches = await dependencies.load_watches(list(request.symbols))
    selected_symbols = [str(row["symbol"]) for row in watches]
    scan_id = dependencies.new_scan_id()
    capacity = dependencies.watchlist_capacity(len(watches))
    if capacity["blocked"]:
        await dependencies.persist_terminal(
            scan_id, observed_at, "blocked", list(request.symbols),
            {"watchlist_capacity": capacity}, {"watched": len(watches)},
        )
        return {
            "status": "blocked", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(),
            "reason": capacity["reason"], "watchlist_capacity": capacity, "alerts": [],
        }
    if not active:
        await dependencies.persist_terminal(
            scan_id, observed_at, "blocked", list(request.symbols),
            {"session": reason}, {"watched": len(watches)},
        )
        return {"status": "blocked", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(), "reason": reason, "alerts": []}
    await dependencies.prune_rule_inputs(observed_at)
    retry_summary = await dependencies.retry_pending_alerts()
    if not watches:
        await dependencies.persist_terminal(
            scan_id, observed_at, "completed", list(request.symbols),
            {"tencent": "skipped"}, {"watched": 0},
        )
        return {
            "status": "completed", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(), "alerts": [],
            "notice": "没有启用的观察/持仓标的；先通过 watchlists API 显式添加。",
        }

    membership_rows = await dependencies.load_exact_memberships(selected_symbols, observed_at)
    mapped_peer_groups = dependencies.mapped_peers(selected_symbols, membership_rows)
    quote_timestamp_slo_seconds = 20.0 if dependencies.high_frequency_window(observed_at) else 45.0
    quote_capture = await dependencies.capture_quotes(selected_symbols, observed_at, quote_timestamp_slo_seconds)
    anomaly_symbols = quote_volume_anomaly_symbols(watches, quote_capture.quotes)
    surge_features, surge_source = await _surge_context_with_priority(
        dependencies.surge_context, watches, mapped_peer_groups, anomaly_symbols,
    )
    surge_source["minute_enrichment_priority"] = {
        "symbols": anomaly_symbols,
        "reason": "quote_volume_anomaly",
        "notice": "只提升分钟证据采集优先级，不单独生成或升级信号。",
    }
    surge_source["exact_watchlist_peer_mapping"] = {
        "status": "completed", "membership_rows": len(membership_rows),
        "symbols_with_mapped_peers": sum(bool(item.get("peer_symbols")) for item in mapped_peer_groups.values()),
        "taxonomy_scope": ["ths_concept_flow", "ths_index_n", "ths_industry"],
        "notice": "仅以同一 taxonomy_key + sector_key 的观察池成员确认；不按名称猜板块关联。",
    }
    peer_contexts = build_peer_contexts(watches, mapped_peer_groups, surge_features, dependencies.peer_context)
    ordered_priority_symbols = anomaly_symbols + [
        str(row["symbol"]) for row in sorted(watches, key=dependencies.watch_priority_key)
        if str(row["symbol"]).upper() not in set(anomaly_symbols)
    ]
    priority_symbols, next_realtime_validation_offset = dependencies.realtime_validation_slice(
        ordered_priority_symbols, request.realtime_validation_offset, request.realtime_validation_limit,
    )
    priority_symbols = inject_anomaly_rotation_priority(
        priority_symbols, anomaly_symbols, request.realtime_validation_limit,
    )
    tushare_minutes = await dependencies.tushare_minutes(priority_symbols) if priority_symbols else {}
    fast_confirmations = await dependencies.fast_confirmations(selected_symbols, quote_capture.quotes, observed_at)
    board_cache_evidence = await dependencies.board_cache_evidence(observed_at)
    source_status = dependencies.build_source_status(
        selected_symbols=selected_symbols, quotes=quote_capture.quotes, all_a_rows=quote_capture.all_a_rows,
        fresh_watch_rows=quote_capture.fresh_watch_rows, sina_watch_rows=quote_capture.sina_watch_rows,
        eastmoney_watch_flow_rows=quote_capture.eastmoney_watch_flow_rows,
        eastmoney_watch_flow_status=quote_capture.eastmoney_watch_flow_status,
        derived_flow_status=quote_capture.derived_flow_status,
        all_a_snapshot_status=quote_capture.all_a_snapshot_status, surge_source=surge_source,
        priority_symbols=priority_symbols, rotation_pool_size=len(ordered_priority_symbols),
        rotation_start_offset=(request.realtime_validation_offset % len(ordered_priority_symbols)
                               if ordered_priority_symbols else 0),
        next_rotation_offset=next_realtime_validation_offset, tushare_minutes=tushare_minutes,
        fast_confirmations=fast_confirmations, board_cache_evidence=board_cache_evidence,
        quote_timestamp_slo_seconds=quote_timestamp_slo_seconds,
    )
    signals = await dependencies.persist_signals(
        scan_id, observed_at, selected_symbols, source_status, watches, quote_capture.quotes,
        quote_capture.all_a_rows, quote_capture.latency_ms, tushare_minutes, surge_features,
        peer_contexts, fast_confirmations,
    )
    shadow_observation: dict[str, Any] = {"status": "standby", "reason": "awaiting_next_minute_rotation"}
    if dependencies.shadow_rotation_due(observed_at):
        try:
            shadow_pool = await dependencies.shadow_pool()
            rotation_candidates, rotation_offset = dependencies.shadow_rotation_slice(
                list(shadow_pool.get("candidates") or []), observed_at,
            )
            if shadow_pool.get("run") and rotation_candidates:
                shadow_symbols = [str(item["symbol"]).upper() for item in rotation_candidates]
                shadow_memberships = await dependencies.load_exact_memberships(shadow_symbols, observed_at)
                shadow_mapped_peers = dependencies.mapped_peers(shadow_symbols, shadow_memberships)
                shadow_watches = [
                    {"symbol": symbol, "metadata": {"surge_strategy": {"enabled": True}}}
                    for symbol in shadow_symbols
                ]
                shadow_quotes, shadow_quote_status = await dependencies.capture_shadow_quotes(shadow_symbols)
                shadow_minutes, shadow_minute_status = await dependencies.surge_context(
                    shadow_watches, mapped_peers=shadow_mapped_peers,
                )
                shadow_peer_contexts = build_peer_contexts(
                    shadow_watches, shadow_mapped_peers, shadow_minutes, dependencies.peer_context,
                )
                shadow_observation = await dependencies.persist_shadow_observations(
                    scan_id=scan_id, observed_at=observed_at, pool=shadow_pool,
                    candidates=rotation_candidates, all_a_rows=quote_capture.all_a_rows,
                    quotes=shadow_quotes,
                    minute_features=shadow_minutes, peer_contexts=shadow_peer_contexts,
                )
                shadow_observation = {
                    **shadow_observation, "rotation_offset": rotation_offset,
                    "rotation_pool_size": len(shadow_pool.get("candidates") or []),
                    "quote_source": shadow_quote_status,
                    "minute_source": shadow_minute_status,
                }
            else:
                shadow_observation = {"status": "standby", "reason": "no_completed_daily_shadow_cohort"}
        except Exception as error:  # noqa: BLE001 - preserve the independent primary scan
            shadow_observation = {"status": "degraded", "reason": str(error)[:240]}
        await dependencies.persist_shadow_status(scan_id, shadow_observation)
    if shadow_observation.get("status") != "standby":
        source_status["ten_day_leader_rotation_shadow"] = shadow_observation
    # Leader-flow research runs on the all-A cross-section this scan already
    # fetched, so it adds no provider call.  Like the rotation shadow above it
    # is fully independent: any failure is recorded and the primary watchlist
    # scan continues untouched.
    xiaojie_observation: dict[str, Any] = {"status": "disabled"}
    if dependencies.xiaojie_leader_flow is not None:
        try:
            xiaojie_observation = await dependencies.xiaojie_leader_flow(
                scan_id=scan_id, observed_at=observed_at, all_a_rows=quote_capture.all_a_rows,
            )
        except Exception as error:  # noqa: BLE001 - never fail the primary scan
            xiaojie_observation = {"status": "degraded", "reason": str(error)[:240]}
        source_status["xiaojie_leader_flow"] = xiaojie_observation
        # The row was written with the primary signals above, so the in-memory
        # mutation alone never reaches the database.
        if dependencies.persist_xiaojie_status is not None:
            await dependencies.persist_xiaojie_status(scan_id, xiaojie_observation)
    alerts: list[dict[str, Any]] = []
    for signal in signals:
        if signal["state"] != "confirmed":
            continue
        delivery = await dependencies.deliver_alert(
            signal["signal_event_id"],
            dependencies.alert_text(
                signal, signal["watch"], signal["quote"] or {}, signal["minute"],
                decision_card_url=dependencies.decision_card_url(signal["symbol"]),
            ),
        )
        alerts.append({
            "signal_event_id": str(signal["signal_event_id"]), "symbol": signal["symbol"],
            "signal_type": signal["signal_type"], "severity": signal["severity"], "delivery": delivery,
        })
    return {
        "status": "completed", "scan_id": str(scan_id), "observed_at": observed_at.isoformat(),
        "source_status": source_status,
        "signals": [{key: value for key, value in signal.items() if key not in {"watch", "quote"}} for signal in signals],
        "alerts": alerts,
        "realtime_validation": {
            "pool_size": len(ordered_priority_symbols), "requested_symbols": priority_symbols,
            "next_offset": next_realtime_validation_offset,
        },
        "delivery_retry": retry_summary,
        "ten_day_leader_rotation_shadow": shadow_observation,
        "xiaojie_leader_flow": xiaojie_observation,
        "notice": "仅为人工复核提醒，不构成交易指令；系统不会自动下单。",
    }


__all__ = ["IntradayWatchlistScanDependencies", "build_peer_contexts", "run_watchlist_scan"]
