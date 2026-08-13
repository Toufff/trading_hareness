"""Pure descriptive aggregation for intraday outcome cohorts."""

from __future__ import annotations

from datetime import datetime
from statistics import mean
from typing import Any, Callable
from zoneinfo import ZoneInfo

def outcome_attribution_summary(items: list[dict[str, Any]], *, number: Callable[[Any], float | None]) -> dict[str, Any]:
    """Aggregate descriptive cohorts while keeping the validation gate closed."""
    dimensions = ("model_version", "stage", "market_state", "sector_linkage", "volume_baseline",
                  "microstructure_state", "price_volume_state", "smart_money_state")
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        attribution = item.get("attribution") if isinstance(item.get("attribution"), dict) else {}
        for dimension in dimensions:
            groups.setdefault((dimension, str(attribution.get(dimension) or "unknown"), str(item.get("horizon_key") or "unknown")), []).append(item)
    summaries: list[dict[str, Any]] = []
    for (dimension, cohort, horizon_key), rows in groups.items():
        matured = [row for row in rows if row.get("status") == "matured" and number(row.get("raw_return")) is not None]
        returns = [float(number(row.get("raw_return")) or 0) for row in matured]
        mfes = [float(value) for row in matured if (value := number(row.get("maximum_favorable_excursion"))) is not None]
        maes = [float(value) for row in matured if (value := number(row.get("maximum_adverse_excursion"))) is not None]
        gains, losses = [value for value in returns if value > 0], [value for value in returns if value < 0]
        payoff_ratio = mean(gains) / abs(mean(losses)) if gains and losses and mean(losses) else None
        summaries.append({"dimension": dimension, "cohort": cohort, "horizon_key": horizon_key,
                          "rows": len(rows), "matured": len(matured),
                          "hit_rate": round(sum(value > 0 for value in returns) / len(returns), 4) if returns else None,
                          "avg_directional_return": round(mean(returns), 8) if returns else None,
                          "avg_mfe": round(mean(mfes), 8) if mfes else None,
                          "avg_mae": round(mean(maes), 8) if maes else None,
                          "payoff_ratio": round(payoff_ratio, 4) if payoff_ratio is not None else None,
                          "evaluation_status": "cohort_reviewable" if len(matured) >= 30 else "descriptive_only",
                          "minimum_reviewable_samples": 30})
    unique_signals = {str(item.get("signal_event_id")) for item in items if item.get("status") == "matured"}
    trading_dates = {item["observed_at"].astimezone(ZoneInfo("Asia/Shanghai")).date()
                     for item in items if item.get("status") == "matured" and isinstance(item.get("observed_at"), datetime)}
    validation_ready = len(unique_signals) >= 200 and len(trading_dates) >= 60
    order = {name: index for index, name in enumerate(dimensions)}
    summaries.sort(key=lambda item: (order.get(item["dimension"], 99), item["horizon_key"], -item["matured"], item["cohort"]))
    return {"items": summaries,
            "validation_gate": {"status": "ready_for_formal_validation" if validation_ready else "accumulating",
                                "matured_unique_signals": len(unique_signals), "trading_days": len(trading_dates),
                                "required_unique_signals": 200, "required_trading_days": 60}}


__all__ = ["outcome_attribution_summary"]
