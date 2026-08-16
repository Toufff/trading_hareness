"""Explainable intraday action context and conservatively shrunk probabilities.

Scores are not probabilities.  This module only exposes an estimated
probability when a matching outcome ledger already contains matured rows.  It
uses trading days as the effective sample size so many correlated symbols on
one market day cannot masquerade as independent evidence.
"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from time import monotonic
from typing import Any, Iterable

from .episode_lifecycle import strategy_family
from .probability_calibration import shrunk_probability_interval


PROBABILITY_PRIOR_STRENGTH = 20.0
PROBABILITY_PRIOR_RATE = 0.50
PROBABILITY_PROFILE_CACHE_SECONDS = 60.0
# A raw hit rate, even after beta shrinkage, is not a calibrated forecast.
# These gates match the live strategy promotion gate.  Until P3 supplies an
# out-of-fold calibration artifact, the number is kept as a *historical
# conditional baseline* rather than exposed as a probability in a Feishu
# decision card.
MIN_CALIBRATED_PROBABILITY_ROWS = 200
MIN_CALIBRATED_PROBABILITY_DAYS = 60
_PROFILE_CACHE_LOCK = Lock()
_PROFILE_CACHE: dict[str, Any] = {"loaded_at": 0.0, "profiles": {}}


def invalidate_intraday_probability_profiles() -> None:
    """Drop derived profiles after an outcome/attribution backfill."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE.update({"loaded_at": 0.0, "profiles": {}})


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def shrunk_probability(
    *, raw_positive_rate: float | None, sample_rows: int, independent_days: int,
    average_directional_return: float | None, horizon: str, source: str,
    prior_rate: float = PROBABILITY_PRIOR_RATE,
    prior_strength: float = PROBABILITY_PRIOR_STRENGTH,
    outcome_definition: str = "signal_direction_return_above_zero",
) -> dict[str, Any]:
    """Build a governed probability estimate from already mature evidence."""
    rows = max(0, int(sample_rows or 0))
    days = max(0, int(independent_days or 0))
    rate = None if raw_positive_rate is None else min(1.0, max(0.0, float(raw_positive_rate)))
    estimate = None
    if rows and days and rate is not None:
        estimate = (prior_rate * prior_strength + rate * days) / (prior_strength + days)
    # This function deliberately has no access to an out-of-fold calibration
    # artifact.  Do not let a small (or even merely large) in-sample outcome
    # ledger masquerade as a calibrated event probability.
    confidence = "unavailable" if not rows else "uncalibrated"
    interval = shrunk_probability_interval(
        raw_positive_rate=rate, independent_days=days,
        prior_rate=prior_rate, prior_strength=prior_strength,
    )
    return {
        "estimated_probability": None,
        "historical_condition_baseline": round(estimate, 4) if estimate is not None else None,
        "raw_positive_rate": round(rate, 4) if rate is not None else None,
        "sample_rows": rows,
        "independent_trading_days": days,
        "average_directional_return": (
            round(float(average_directional_return), 6)
            if average_directional_return is not None else None
        ),
        "horizon": horizon,
        "source": source,
        "confidence_tier": confidence,
        "calibration_status": "not_run",
        "display_eligible": False,
        "probability_gate": {
            "required_rows": MIN_CALIBRATED_PROBABILITY_ROWS,
            "required_independent_trading_days": MIN_CALIBRATED_PROBABILITY_DAYS,
            "requires": "out_of_fold_calibration_artifact",
        },
        "outcome_definition": outcome_definition,
        "method": "beta_shrinkage_with_trading_day_effective_sample_size",
        "prior_rate": prior_rate,
        "prior_strength": prior_strength,
        "confidence_interval_lower": interval["lower"],
        "confidence_interval_upper": interval["upper"],
        "confidence_interval_method": interval["method"],
        "confidence_interval_effective_trials": interval["effective_trials"],
        "notice": "历史条件基准，不是已校准概率；评分不参与概率换算。",
    }


def _govern_probability_display(profile: dict[str, Any]) -> dict[str, Any]:
    """Keep uncalibrated numeric estimates out of human-facing probabilities.

    Some strategy experiments persist a prior ``research_probability``.  The
    scanner must apply the same gate to those rows as to its live outcome
    profile; otherwise a diagnostic value could bypass the shared policy.
    """
    result = dict(profile)
    rows = max(0, int(result.get("sample_rows") or 0))
    days = max(0, int(result.get("independent_trading_days") or 0))
    calibration_status = str(result.get("calibration_status") or "not_run")
    candidate = _number(result.get("estimated_probability"))
    if result.get("historical_condition_baseline") is None and candidate is not None:
        result["historical_condition_baseline"] = round(candidate, 4)
    eligible = bool(
        calibration_status == "validated"
        and rows >= MIN_CALIBRATED_PROBABILITY_ROWS
        and days >= MIN_CALIBRATED_PROBABILITY_DAYS
        and candidate is not None
    )
    result["display_eligible"] = eligible
    if not eligible:
        result["estimated_probability"] = None
        result["confidence_tier"] = "unavailable" if not rows else "uncalibrated"
        result["calibration_status"] = calibration_status
        result["probability_gate"] = {
            "required_rows": MIN_CALIBRATED_PROBABILITY_ROWS,
            "required_independent_trading_days": MIN_CALIBRATED_PROBABILITY_DAYS,
            "requires": "out_of_fold_calibration_artifact",
        }
        result["notice"] = "暂不展示概率：历史条件基准尚未通过时间外校准和样本门禁。"
    return result


def probability_profiles_from_rows(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate one mature 30-minute row per event into family/type profiles."""
    materialized = [dict(raw) for raw in rows]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    global_by_type_day: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in materialized:
        family = strategy_family(str(row.get("signal_key") or ""))
        signal_type = str(row.get("signal_type") or "watch")
        grouped[(family, signal_type)].append(row)
        raw_return = _number(row.get("raw_return"))
        if raw_return is not None:
            global_by_type_day[signal_type][str(row.get("exchange_date") or "unknown")].append(
                1.0 if raw_return > 0 else 0.0
            )
    global_prior_rates = {
        signal_type: sum(sum(values) / len(values) for values in days.values()) / len(days)
        for signal_type, days in global_by_type_day.items() if days
    }
    profiles: dict[str, dict[str, Any]] = {}
    for (family, signal_type), items in grouped.items():
        usable = [item for item in items if _number(item.get("raw_return")) is not None]
        by_day: dict[str, list[float]] = defaultdict(list)
        for item in usable:
            by_day[str(item.get("exchange_date") or "unknown")].append(
                1.0 if float(item["raw_return"]) > 0 else 0.0
            )
        daily_rates = [sum(values) / len(values) for values in by_day.values() if values]
        raw_rate = sum(float(item["raw_return"]) > 0 for item in usable) / len(usable) if usable else None
        avg_return = sum(float(item["raw_return"]) for item in usable) / len(usable) if usable else None
        # The posterior uses the mean daily hit rate with the number of days as
        # effective N, not the more optimistic number of cross-sectional rows.
        daily_rate = sum(daily_rates) / len(daily_rates) if daily_rates else raw_rate
        profiles[f"{family}:{signal_type}"] = shrunk_probability(
            raw_positive_rate=daily_rate,
            sample_rows=len(usable), independent_days=len(daily_rates),
            average_directional_return=avg_return, horizon="30m",
            source="matured_intraday_signal_outcomes",
            prior_rate=global_prior_rates.get(signal_type, PROBABILITY_PRIOR_RATE),
        ) | {
            "raw_event_positive_rate": round(raw_rate, 4) if raw_rate is not None else None,
            "prior_source": "all_matured_30m_outcomes_for_signal_type",
        }
    return profiles


def load_intraday_probability_profiles(
    connection: Any, *, cache_ttl_seconds: float = PROBABILITY_PROFILE_CACHE_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Read bounded past outcomes and reuse the projection for one minute."""
    now = monotonic()
    with _PROFILE_CACHE_LOCK:
        cached = _PROFILE_CACHE.get("profiles")
        loaded_at = float(_PROFILE_CACHE.get("loaded_at") or 0)
        if loaded_at > 0 and now - loaded_at < max(0.0, cache_ttl_seconds):
            return cached
        rows = connection.execute(
            """SELECT e.signal_key,e.signal_type,
                      (e.observed_at AT TIME ZONE 'Asia/Shanghai')::date AS exchange_date,
                      o.raw_return
                 FROM quant.intraday_signal_outcomes o
                 JOIN quant.intraday_signal_events e ON e.signal_event_id=o.signal_event_id
                WHERE o.horizon_key='30m' AND o.status='matured' AND o.raw_return IS NOT NULL
                  AND e.observed_at>=now()-interval '365 days'
                ORDER BY e.observed_at"""
        ).fetchall()
        profiles = probability_profiles_from_rows(dict(row) for row in rows)
        _PROFILE_CACHE.update({"loaded_at": now, "profiles": profiles})
        return profiles


def probability_for_signal(
    signal: dict[str, Any], profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    preregistered = conditions.get("research_probability")
    if isinstance(preregistered, dict):
        return _govern_probability_display(preregistered)
    family = strategy_family(str(signal.get("signal_key") or ""))
    key = f"{family}:{signal.get('signal_type') or 'watch'}"
    profile = profiles.get(key) or shrunk_probability(
        raw_positive_rate=None, sample_rows=0, independent_days=0,
        average_directional_return=None, horizon="30m",
        source="no_matching_matured_outcomes",
    )
    return _govern_probability_display(profile)


def decision_context(signal: dict[str, Any], probability: dict[str, Any]) -> dict[str, Any]:
    """Explain why a human should inspect an entry/reduce/exit alert."""
    conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    setup = str(conditions.get("setup") or "")
    signal_type = str(signal.get("signal_type") or "watch")
    minute = conditions.get("minute_features") if isinstance(conditions.get("minute_features"), dict) else {}
    peers = conditions.get("peer_context") if isinstance(conditions.get("peer_context"), dict) else {}
    setup_state = conditions.get("setup_state") if isinstance(conditions.get("setup_state"), dict) else {}
    reasons: list[str] = []
    if setup == "countertrend_rebound_confirmed_plus_intraday_acceptance":
        reasons.append("日线已进入B浪反弹确认态，而不是恐慌或单日试探")
        reasons.append(
            f"盘中3分钟动量 {minute.get('return_3m_pct', '—')}%，价格位于VWAP上方 "
            f"{minute.get('above_vwap_pct', '—')}%"
        )
        reasons.append(
            f"量能倍数 {max(_number(conditions.get('volume_ratio')) or 0, _number(minute.get('minute_volume_multiple')) or 0):.2f}，"
            f"同板块确认 {peers.get('confirming_peer_count', 0)} 只，资金流 {conditions.get('main_net_inflow', '—')}"
        )
    elif setup == "countertrend_rebound_intraday_acceptance_failure":
        reasons.append("反弹未能维持VWAP承接，短周期动量已转弱")
        reasons.append(
            f"3分钟动量 {minute.get('return_3m_pct', '—')}%，VWAP偏离 {minute.get('above_vwap_pct', '—')}%，"
            f"相对持仓成本 {conditions.get('return_since_entry_pct', '—')}%"
        )
        if conditions.get("peer_confirmation_lost"):
            reasons.append("同源精确板块观察池中未见同向确认，原共振已消失")
        if conditions.get("cost_risk_lost"):
            reasons.append("回撤已触及反弹仓位的风险复核阈值")
    elif setup == "minute_price_volume_plus_sector_breadth":
        reasons.append("分钟价量突破与板块成分股同步上行")
    elif setup == "leader_minute_burst":
        reasons.append("个股率先出现分钟级放量脉冲，板块尚待确认")
    elif setup == "eac_first_intraday_high":
        reasons.append("首次突破日内高点并出现量能扩张，等待承接确认")
    elif setup == "eac_acceptance_confirmed":
        reasons.append("首轮拉升后未快速跌落，价格维持在VWAP上方")
    elif setup == "deep_reversal_impulse_from_limit_down_zone":
        reasons.append("从深跌区域出现放量回收，但尚未站回昨收")
    elif setup == "deep_reversal_previous_close_reclaim":
        reasons.append("深跌反转后重新站回昨收，并获得资金流或同板块确认")
    elif setup == "green_reclaim_price_volume_vwap":
        reasons.append("从日内低点回收、翻红并重新站上VWAP")
    if conditions.get("hard_stop") is not None:
        reasons.append(f"现价已触及硬止损 {conditions.get('hard_stop')}")
    if setup_state.get("state") in {"policy_constrained", "data_blocked", "evidence_incomplete"}:
        reasons.append(f"实时状态为 {setup_state.get('state')}：{'、'.join(str(item) for item in setup_state.get('reasons') or [])}")
    if conditions.get("flow_extreme") == "bottom_1pct":
        reasons.append("主力流指标跌入全市场后1%，且量能放大")
    elif conditions.get("flow_extreme") == "top_1pct":
        reasons.append("主力流指标升入全市场前1%，且量能放大")
    if signal_type == "reduce" and conditions.get("entry_price") is not None:
        reasons.append(f"价格相对成本 {conditions.get('entry_price')} 回撤且资金流为负")
    if not reasons:
        reasons.append(
            f"价格变化 {conditions.get('pct_change', '—')}%，量比 {conditions.get('volume_ratio', '—')}，"
            f"主力流 {conditions.get('main_net_inflow', '—')}"
        )

    if signal_type == "entry":
        action = "入场复核"
        invalidations = ["跌回VWAP且3分钟动量转负", "板块共振消失或资金流由正转负", "市场/组合风险门禁转为阻止"]
    elif signal_type == "reduce":
        action = "减仓复核"
        invalidations = ["卖出资金流快速修复并重新站回VWAP", "若当日买入且可卖数量为0，仅保留风险提醒"]
    elif signal_type == "exit":
        action = "离场复核"
        invalidations = ["确认价格是否为跨源一致的有效跌破", "若可卖数量为0或跌停不可成交，仅保留风险提醒"]
    else:
        action = "观察复核"
        invalidations = ["量价脉冲未延续", "板块或第二时间样本未确认"]
    return {
        "action": action,
        "direction": "up" if signal_type in {"entry", "watch"} else "down",
        "reasons": reasons[:4],
        "invalidations": invalidations,
        "probability": probability,
        "notice": "原因和概率只帮助人工复核，不构成自动交易指令。",
    }


__all__ = [
    "decision_context", "invalidate_intraday_probability_profiles", "load_intraday_probability_profiles", "probability_for_signal",
    "probability_profiles_from_rows", "shrunk_probability",
]
