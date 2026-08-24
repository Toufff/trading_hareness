"""One bounded persistence action for intraday ten-day shadow observations."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class TenDayLeaderRotationIntradayDependencies:
    database: Any
    quote_from_tencent: Callable[[dict[str, Any]], dict[str, Any] | None]
    quote_source: Callable[[dict[str, Any] | None], str]
    market_context_batch: Callable[[Any, list[tuple[datetime, str]]], dict[tuple[datetime, str], dict[str, Any]]]
    evaluate: Callable[..., list[dict[str, Any]]]
    persist: Callable[..., int]
    json_safe: Callable[[Any], Any]


def persist_ten_day_leader_rotation_intraday(
    *, scan_id: Any, observed_at: datetime, pool: dict[str, Any], candidates: list[dict[str, Any]],
    tencent_rows: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], minute_features: dict[str, dict[str, Any]],
    peer_contexts: dict[str, dict[str, Any]], dependencies: TenDayLeaderRotationIntradayDependencies,
) -> dict[str, Any]:
    """Evaluate only the bounded selected slice; no alert or order path exists."""
    run = dict(pool.get("run") or {})
    if not run or not candidates:
        return {"status": "skipped", "reason": "no_completed_daily_shadow_cohort", "observed": 0}
    quote_by_symbol: dict[str, dict[str, Any]] = {
        str(symbol).upper(): dict(value) for symbol, value in quotes.items()
    }
    for raw in tencent_rows:
        normalized = dependencies.quote_from_tencent(dict(raw))
        if normalized and normalized.get("symbol"):
            quote_by_symbol[str(normalized["symbol"]).upper()] = normalized
    symbols = [str(item.get("symbol") or "").upper() for item in candidates]
    with dependencies.database.transaction() as connection:
        contexts = dependencies.market_context_batch(connection, [(observed_at, symbol) for symbol in symbols])
        observations = dependencies.evaluate(
            run=run, candidates=candidates, observed_at=observed_at, quotes=quote_by_symbol,
            minute_features=minute_features, peer_contexts=peer_contexts,
            market_contexts={symbol: contexts.get((observed_at, symbol), {}) for symbol in symbols},
            quote_source=dependencies.quote_source,
        )
        written = dependencies.persist(
            connection, run_id=run["run_id"], scan_id=scan_id, observations=observations,
            json_safe=dependencies.json_safe,
        )
        summary = {
            "status": "completed", "run_id": str(run["run_id"]), "observed": written,
            "shadow_eligible": sum(bool(item["shadow_eligible"]) for item in observations),
            "states": dict(sorted(Counter(str(item["shadow_state"]) for item in observations).items())),
            "scope": "research_only_no_orders",
        }
    return summary


__all__ = ["TenDayLeaderRotationIntradayDependencies", "persist_ten_day_leader_rotation_intraday"]
