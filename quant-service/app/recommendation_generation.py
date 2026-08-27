"""Point-in-time recommendation materialization and scoring."""

from __future__ import annotations

import math
import uuid
from datetime import date, timedelta
from typing import Any, Callable

from .stable_json import stable_json


def generate(
    request: Any,
    *,
    cn_today: Callable[[], date],
    build_feature_snapshot: Callable[[date, str], dict[str, Any]],
    analyst_execution_context: Callable[[Any, date], dict[str, Any]],
    ablation_scores: Callable[..., dict[str, float]],
    number: Callable[[Any, float | None], float | None],
    db: Any,
    model_version: str,
    feature_version: str,
    json_safe: Callable[[Any], Any],
) -> dict[str, Any]:
    as_of_date = request.as_of_date or cn_today()
    run_id = uuid.uuid4()
    materialized = build_feature_snapshot(as_of_date, request.universe_key)
    regime = str(materialized["market_regime"])
    candidates: list[dict[str, Any]] = []
    with db.transaction() as connection:
        analyst_context = analyst_execution_context(connection, as_of_date)
        analyst_weight = float(analyst_context.get("max_live_weight") or 0.0) if analyst_context["execution_eligible"] else 0.0
        for item in materialized["items"]:
            feature = item["features"]
            flags = list(item["quality_flags"])
            close, sma20 = number(feature.get("close"), None), number(feature.get("sma_20"), None)
            return_5, return_20 = number(feature.get("return_5"), None), number(feature.get("return_20"), None)
            flow_rate = number((feature.get("moneyflow_dc") or {}).get("net_amount_rate"), None)
            analyst = feature.get("analyst") or {}
            consensus, skill = number(analyst.get("consensus"), None), number(analyst.get("analyst_skill"), 0.5) or 0.5
            trend = 0.35 if sma20 and close > sma20 else -0.35 if sma20 else 0.0
            quant_signal = trend + 0.25 * math.tanh((return_5 or 0) * 12) + 0.25 * math.tanh((return_20 or 0) * 7) + 0.15 * math.tanh((flow_rate or 0) / 3)
            analyst_signal = (consensus or 0) * max(0.4, min(0.8, skill + 0.2))
            applied_analyst_weight = analyst_weight if analyst.get("claim_count") else 0.0
            has_analyst_evidence = bool(analyst.get("claim_count"))
            if has_analyst_evidence and not applied_analyst_weight:
                flags.append("analyst_research_only")
            risk_penalty = 0.08 if regime == "risk_off" and quant_signal > 0 else 0.0
            if risk_penalty:
                flags.append("risk_off_regime")
            ablation = ablation_scores(market_signal=quant_signal,
                                       analyst_signal=analyst_signal if consensus is not None else None,
                                       has_analyst_evidence=has_analyst_evidence,
                                       applied_weight=applied_analyst_weight,
                                       risk_penalty=risk_penalty)
            signal = (ablation["applied_score"] - 50.0) / 50.0
            hard_flags = {"ST", "suspended", "missing_market_data", "insufficient_history_20", "adj_factor_missing", "corporate_action_unresolved"}
            penalty = min(0.35, 0.07 * len(set(flags)))
            score = max(0.0, min(100.0, 50 + 50 * signal - 100 * penalty))
            direction = 1 if signal >= 0.14 else -1 if signal <= -0.14 else 0
            decision = "research_candidate" if direction > 0 and score >= 58 and not hard_flags.intersection(flags) else "watch"
            if direction < 0 or hard_flags.intersection(flags):
                decision = "no_trade" if direction < 0 or {"ST", "suspended", "missing_market_data"}.intersection(flags) else "watch"
            coverage = min(1.0, (number(feature.get("bar_count"), 0) or 0) / 21)
            source_count = 1 + int("fundamentals" in feature) + int("moneyflow_dc" in feature) + int("moneyflow" in feature)
            confidence = max(0.0, min(1.0, 0.35 + 0.35 * coverage + 0.08 * source_count + 0.08 * min(1, number(analyst.get("claim_count"), 0) or 0)))
            invalidation = ["close_below_sma_20", "data_stale", "suspended_or_ST"]
            if has_analyst_evidence:
                invalidation.append("analyst_consensus_reverses")
            candidates.append({"symbol": item["symbol"], "score": round(score, 2), "decision": decision, "direction": direction,
                "confidence": round(confidence, 3), "flags": sorted(set(flags)), "quant_signal": round(quant_signal, 5),
                "analyst_consensus": consensus, "analyst_skill": skill, "analyst_weight": applied_analyst_weight,
                "analyst_signal": round(analyst_signal, 5) if consensus is not None else None,
                "market_only_score": round(max(0.0, min(100.0, ablation["market_only_score"] - 100 * penalty)), 2),
                "analyst_shadow_score": round(max(0.0, min(100.0, ablation["analyst_shadow_score"] - 100 * penalty)), 2),
                "momentum_5": return_5, "momentum_20": return_20, "moneyflow_net_amount_rate": flow_rate,
                "evidence": analyst.get("evidence", []), "signal_count": analyst.get("claim_count", 0),
                "trading_date": feature.get("market_data_date"), "invalidation": invalidation})
        candidates.sort(key=lambda item: (item["decision"] != "research_candidate", -item["score"], item["symbol"]))
        connection.execute(
            """INSERT INTO quant.recommendation_runs(run_id,as_of_date,model_version,market_regime,source_status,snapshot_key,status,model_metadata)
               VALUES(%s,%s,%s,%s,%s,%s,'completed',%s)""",
            (run_id, as_of_date, model_version, regime, stable_json({"candidate_inputs": len(candidates), "universe_key": request.universe_key}),
             materialized["snapshot_key"], stable_json({"feature_version": feature_version, "horizon_days": request.horizon_days,
                                                   "analyst_execution_context": analyst_context, "analyst_weight_cap": analyst_weight})),
        )
        for rank, candidate in enumerate(candidates[:request.limit], start=1):
            breakdown = {key: candidate[key] for key in ("quant_signal", "analyst_consensus", "analyst_skill", "analyst_weight", "momentum_5", "momentum_20", "moneyflow_net_amount_rate")}
            explanation = {"signal_count": candidate["signal_count"], "evidence": candidate["evidence"], "market_data_date": candidate["trading_date"], "notice": "研究候选池，不构成自动交易指令"}
            connection.execute(
                """INSERT INTO quant.recommendations(run_id,rank,symbol,decision,score,score_breakdown,explanation,risk_flags,direction,horizon_days,confidence,valid_until,invalidation)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, rank, candidate["symbol"], candidate["decision"], candidate["score"], stable_json(breakdown), stable_json(explanation), stable_json(candidate["flags"]), candidate["direction"], request.horizon_days, candidate["confidence"], as_of_date + timedelta(days=request.horizon_days), stable_json(candidate["invalidation"])),
            )
            connection.execute(
                """INSERT INTO quant.strategy_ablation_observations(run_id,symbol,market_only_score,analyst_shadow_score,applied_score,market_signal,analyst_signal,analyst_delta,applied_analyst_weight,analyst_execution_status,evidence)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(run_id,symbol) DO UPDATE SET market_only_score=EXCLUDED.market_only_score,analyst_shadow_score=EXCLUDED.analyst_shadow_score,applied_score=EXCLUDED.applied_score,market_signal=EXCLUDED.market_signal,analyst_signal=EXCLUDED.analyst_signal,analyst_delta=EXCLUDED.analyst_delta,applied_analyst_weight=EXCLUDED.applied_analyst_weight,analyst_execution_status=EXCLUDED.analyst_execution_status,evidence=EXCLUDED.evidence""",
                (run_id, candidate["symbol"], candidate["market_only_score"], candidate["analyst_shadow_score"], candidate["score"], candidate["quant_signal"], candidate["analyst_signal"], round(candidate["analyst_shadow_score"] - candidate["market_only_score"], 5), candidate["analyst_weight"], str(analyst_context.get("status") or "disabled"), stable_json({"execution_eligible": bool(analyst_context.get("execution_eligible")), "analyst_claim_count": candidate["signal_count"], "live_effect": "none"})),
            )
    return {"run_id": str(run_id), "as_of_date": str(as_of_date), "market_regime": regime, "snapshot_key": materialized["snapshot_key"], "recommendations": candidates[:request.limit]}


__all__ = ["generate"]
