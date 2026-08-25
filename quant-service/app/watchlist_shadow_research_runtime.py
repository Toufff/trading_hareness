"""Transactional adapter for paired watchlist shadow-model research."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class WatchlistShadowResearchRuntimeDependencies:
    database: Any
    main_wave_research: Callable[[Any, date | None], dict[str, Any]]
    rebound_research: Callable[[Any, date | None], dict[str, Any]]
    main_wave_key: str
    rebound_key: str
    china_today: Callable[[], date]
    json_safe: Callable[[Any], Any]
    json_value: Callable[[Any], Any]


class WatchlistShadowResearchRuntime:
    """Persist the paired, research-only model receipts atomically."""

    def __init__(self, dependencies: WatchlistShadowResearchRuntimeDependencies) -> None:
        self._dependencies = dependencies

    def persist(self, payload: Any) -> dict[str, Any]:
        dependencies = self._dependencies
        with dependencies.database.transaction() as connection:
            results = (
                dependencies.main_wave_research(connection, payload.as_of_date),
                dependencies.rebound_research(connection, payload.as_of_date),
            )
            persisted: dict[str, dict[str, Any]] = {}
            for model_result in results:
                row = connection.execute(
                    """INSERT INTO quant.strategy_experiments(
                           strategy_key,universe_key,start_date,end_date,status,parameters,metrics,equity_curve,trades)
                       VALUES(%s,'watchlist',%s,%s,%s,%s,%s,%s,%s)
                       RETURNING strategy_experiment_id,created_at""",
                    (
                        model_result["strategy_key"],
                        model_result.get("start_date") or payload.as_of_date or dependencies.china_today(),
                        model_result.get("end_date") or payload.as_of_date or dependencies.china_today(),
                        model_result["status"],
                        dependencies.json_value(dependencies.json_safe(model_result.get("parameters", {}))),
                        dependencies.json_value(dependencies.json_safe(model_result.get("metrics", {}))),
                        dependencies.json_value(dependencies.json_safe(model_result.get("equity_curve", []))),
                        dependencies.json_value(dependencies.json_safe(model_result.get("trades", []))),
                    ),
                ).fetchone()
                persisted[model_result["strategy_key"]] = {
                    **model_result,
                    "strategy_experiment_id": str(row["strategy_experiment_id"]),
                    "created_at": row["created_at"],
                }
        return {
            **persisted[dependencies.main_wave_key],
            "countertrend_rebound": persisted[dependencies.rebound_key],
        }


__all__ = ["WatchlistShadowResearchRuntime", "WatchlistShadowResearchRuntimeDependencies"]
