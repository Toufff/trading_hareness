"""Theory-anchored challenger for watchlist main-wave onset research.

Version one proved that a static, class-balanced logistic score can become
confident after the market regime has changed.  This challenger therefore
uses a pre-declared price/volume pattern as the qualification gate and is
allowed to select no stock on a trading day.  The old model is still measured
as a frozen baseline; its already-observed test window is diagnostic only and
cannot promote this challenger.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from typing import Any, Iterable

from .watchlist_main_wave import (
    FEATURE_KEYS,
    FEATURE_LABELS,
    HORIZON_DAYS,
    LOOKBACK_DAYS,
    _auc,
    build_examples,
    chronological_splits,
    evaluate_split,
    fit_logistic,
    normalize_bars,
)


MODEL_VERSION = "watchlist-main-wave-pattern-v2"
STRATEGY_KEY = "watchlist_main_wave_shadow_v2"
MAX_DAILY_CANDIDATES = 3

CONFIRMED_THRESHOLDS = {
    "return_20d_min": 0.0,
    "prior_high_20_gap_min": -0.03,
    "volume_ratio_20d_min": 1.20,
    "close_location_min": 0.65,
    "range_5_to_20_max": 1.20,
}
FORMING_THRESHOLDS = {
    "return_20d_min": 0.0,
    "ma20_60_gap_min": 0.0,
    "prior_high_20_gap_min": -0.10,
    "prior_high_20_gap_max": -0.03,
    "volume_5_20_ratio_max": 0.90,
    "range_5_to_20_max": 0.80,
}


def _average(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, lower: float, upper: float) -> float:
    if upper <= lower:
        return 0.0
    return min(1.0, max(0.0, (value - lower) / (upper - lower)))


def main_wave_pattern(features: dict[str, Any]) -> dict[str, Any]:
    """Classify one close-only feature row using transparent causal rules."""
    values = {key: _finite(features.get(key)) for key in FEATURE_KEYS}
    if any(value is None for value in values.values()):
        return {"state": "invalid", "strength": 0.0, "passed": [], "failed": ["finite_features"]}
    numeric = {key: float(value) for key, value in values.items() if value is not None}
    range_ratio = numeric["range_5d"] / numeric["range_20d"] if numeric["range_20d"] > 1e-12 else math.inf
    confirmed = {
        "non_negative_20d_trend": numeric["return_20d"] >= CONFIRMED_THRESHOLDS["return_20d_min"],
        "within_3pct_of_prior_20d_high": numeric["prior_high_20_gap"] >= CONFIRMED_THRESHOLDS["prior_high_20_gap_min"],
        "volume_expansion_1_2x": numeric["volume_ratio_20d"] >= CONFIRMED_THRESHOLDS["volume_ratio_20d_min"],
        "close_in_upper_35pct": numeric["close_location"] >= CONFIRMED_THRESHOLDS["close_location_min"],
        "range_not_blowoff": range_ratio <= CONFIRMED_THRESHOLDS["range_5_to_20_max"],
    }
    forming = {
        "non_negative_20d_trend": numeric["return_20d"] >= FORMING_THRESHOLDS["return_20d_min"],
        "aligned_20_60d_average": numeric["ma20_60_gap"] >= FORMING_THRESHOLDS["ma20_60_gap_min"],
        "below_but_near_prior_high": (
            FORMING_THRESHOLDS["prior_high_20_gap_min"] <= numeric["prior_high_20_gap"]
            < FORMING_THRESHOLDS["prior_high_20_gap_max"]
        ),
        "volume_contraction": numeric["volume_5_20_ratio"] <= FORMING_THRESHOLDS["volume_5_20_ratio_max"],
        "range_contraction": range_ratio <= FORMING_THRESHOLDS["range_5_to_20_max"],
    }
    components = {
        "trend": _clip(numeric["return_20d"], -0.10, 0.20),
        "near_prior_high": _clip(numeric["prior_high_20_gap"], -0.15, 0.0),
        "volume_expansion": _clip(numeric["volume_ratio_20d"], 0.70, 2.50),
        "close_location": _clip(numeric["close_location"], 0.20, 1.0),
        "controlled_range": 1.0 - _clip(range_ratio, 0.50, 2.0),
    }
    strength = _average(components.values())
    checks = confirmed if all(confirmed.values()) else forming
    state = "confirmed" if all(confirmed.values()) else "forming" if all(forming.values()) else "observe"
    return {
        "state": state,
        "strength": round(strength, 8),
        "components": components,
        "range_5_to_20": range_ratio,
        "passed": [key for key, passed in checks.items() if passed],
        "failed": [key for key, passed in checks.items() if not passed],
    }


def _pattern_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for row in rows:
        pattern = main_wave_pattern(row["features"])
        scored.append({**row, "model_score": pattern["strength"], "pattern": pattern})
    return scored


def _select_candidates(scored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        if row["pattern"]["state"] == "confirmed":
            by_date[row["signal_date"]].append(row)
    selected: list[dict[str, Any]] = []
    for values in by_date.values():
        selected.extend(sorted(
            values, key=lambda item: (item["model_score"], item["symbol"]), reverse=True,
        )[:MAX_DAILY_CANDIDATES])
    return selected


def evaluate_pattern_split(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored = _pattern_rows(rows)
    selected = _select_candidates(scored)
    base_rate = _average(item["label"] for item in scored)
    precision = _average(item["label"] for item in selected)
    all_dates = {item["signal_date"] for item in scored}
    candidate_dates = {item["signal_date"] for item in selected}
    return {
        "rows": len(scored),
        "dates": len(all_dates),
        "symbols": len({item["symbol"] for item in scored}),
        "positive_rows": sum(item["label"] for item in scored),
        "base_rate": base_rate,
        "roc_auc": _auc(scored),
        "selected_rows": len(selected),
        "selected_dates": len(candidate_dates),
        "abstained_dates": len(all_dates - candidate_dates),
        "selected_symbols": len({item["symbol"] for item in selected}),
        "selected_positive_rows": sum(item["label"] for item in selected),
        "selected_precision": precision,
        "selected_lift": precision / base_rate if base_rate else None,
        "selected_terminal_return": _average(item["terminal_return"] for item in selected),
        "selected_mfe": _average(item["maximum_favorable_excursion"] for item in selected),
        "selected_mae": _average(item["maximum_adverse_excursion"] for item in selected),
    }, selected


def _feature_direction(rows: list[dict[str, Any]], key: str) -> float:
    positives = [item["features"][key] for item in rows if item["label"] == 1]
    negatives = [item["features"][key] for item in rows if item["label"] == 0]
    return _average(positives) - _average(negatives)


def _monthly_base_rates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["signal_date"].strftime("%Y-%m")].append(row)
    return [
        {"month": month, "rows": len(values), "positive_rate": _average(item["label"] for item in values)}
        for month, values in sorted(groups.items())
    ]


def diagnose_v1_failure(splits: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    model = fit_logistic(splits["train"])
    baseline: dict[str, Any] = {}
    selections: dict[str, list[dict[str, Any]]] = {}
    for key in ("train", "validation", "test"):
        baseline[key], selections[key] = evaluate_split(splits[key], model)
    sign_flips = []
    for key in FEATURE_KEYS:
        train_direction = _feature_direction(splits["train"], key)
        test_direction = _feature_direction(splits["test"], key)
        if train_direction * test_direction < 0:
            sign_flips.append({
                "feature": key,
                "label": FEATURE_LABELS[key],
                "train_positive_minus_negative": train_direction,
                "test_positive_minus_negative": test_direction,
            })
    test_selected = selections["test"]
    mean_selected_score = _average(float(item["model_score"]) for item in test_selected)
    test_precision = float(baseline["test"]["selected_precision"] or 0.0)
    return {
        "frozen_v1_metrics": baseline,
        "monthly_positive_rate": {
            key: _monthly_base_rates(splits[key]) for key in ("train", "validation", "test")
        },
        "base_rate_shift": {
            "train": baseline["train"]["base_rate"],
            "test": baseline["test"]["base_rate"],
            "test_to_train_ratio": (
                baseline["test"]["base_rate"] / baseline["train"]["base_rate"]
                if baseline["train"]["base_rate"] else None
            ),
        },
        "feature_direction_flips": sign_flips,
        "feature_direction_flip_count": len(sign_flips),
        "score_calibration_warning": {
            "mean_selected_score": mean_selected_score,
            "realized_selected_precision": test_precision,
            "optimism_gap": mean_selected_score - test_precision,
        },
        "forced_selection_warning": {
            "v1_selected_dates": baseline["test"]["dates"],
            "reason": "daily_top_quintile selects stocks even when every score is weak or stale",
        },
        "conclusion": (
            "The v1 failure is regime and calibration drift, not a missing permissive threshold. "
            "Its frozen test metrics must not be repaired by tuning on the same dates."
        ),
    }


def _current_pattern_scores(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    state_order = {"confirmed": 0, "forming": 1, "observe": 2, "invalid": 3}
    for item in current:
        pattern = main_wave_pattern(item["features"])
        rows.append({
            **item,
            "model_score": pattern["strength"],
            "pattern": pattern,
            "state": f"shadow_{pattern['state']}",
        })
    ordered = sorted(rows, key=lambda item: (
        -state_order.get(item["pattern"]["state"], 9), item["model_score"], item["symbol"],
    ), reverse=True)
    for index, item in enumerate(ordered):
        item["rank"] = index + 1
        item["percentile"] = 1.0 - index / max(1, len(ordered))
    return ordered


def _non_overlapping_curve(selected: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in selected:
        by_date[row["signal_date"]].append(row)
    equity, curve, last_exit = 1.0, [], None
    for signal_date in sorted(by_date):
        rows = by_date[signal_date]
        if last_exit is not None and signal_date <= last_exit:
            continue
        period_return = _average((1 + item["terminal_return"]) * (1 - 0.0018) ** 2 - 1 for item in rows)
        equity *= 1 + period_return
        last_exit = max(item["exit_date"] for item in rows)
        curve.append({"date": str(signal_date), "return": period_return, "equity": equity, "positions": len(rows)})
    return curve


def research_from_rows_v2(rows: Iterable[dict[str, Any]], start_date: date, end_date: date) -> dict[str, Any]:
    grouped = normalize_bars(rows)
    examples, current = build_examples(grouped)
    splits, split_contract = chronological_splits(examples)
    if any(not splits[key] for key in ("train", "validation", "test")):
        return {
            "status": "insufficient_history",
            "strategy_key": STRATEGY_KEY,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "metrics": {"sample_rows": len(examples), "split_contract": split_contract},
            "parameters": {"model_version": MODEL_VERSION},
            "equity_curve": [],
            "trades": [],
        }
    walk_forward: dict[str, Any] = {}
    selections: dict[str, list[dict[str, Any]]] = {}
    for key in ("train", "validation", "test"):
        walk_forward[key], selections[key] = evaluate_pattern_split(splits[key])
    diagnosis = diagnose_v1_failure(splits)
    test = walk_forward["test"]
    gate_checks = {
        "test_dates_ge_30": test["dates"] >= 30,
        "independent_selected_dates_ge_30": test["selected_dates"] >= 30,
        "selected_positive_rows_ge_30": test["selected_positive_rows"] >= 30,
        "test_lift_ge_1_20": (test["selected_lift"] or 0) >= 1.20,
        "test_selected_return_positive": test["selected_terminal_return"] > 0,
        "test_selected_mae_above_minus_8pct": test["selected_mae"] >= -0.08,
        "fresh_unseen_forward_window": False,
        "point_in_time_unbiased_universe": False,
        "three_year_regime_coverage": False,
        "manual_approval": False,
    }
    promotion_ready = all(gate_checks.values())
    current_scores = _current_pattern_scores(current)
    trades = [
        {
            "symbol": row["symbol"], "name": row.get("name"),
            "signal_date": str(row["signal_date"]), "entry_date": str(row["entry_date"]),
            "exit_date": str(row["exit_date"]), "score": row["model_score"],
            "label": row["label"], "pattern": row["pattern"],
            "terminal_return": row["terminal_return"],
            "maximum_favorable_excursion": row["maximum_favorable_excursion"],
            "maximum_adverse_excursion": row["maximum_adverse_excursion"],
        }
        for row in sorted(selections["test"], key=lambda item: (item["signal_date"], -item["model_score"]))[:500]
    ]
    metrics = {
        "sample_rows": len(examples),
        "evaluable_dates": len({item["signal_date"] for item in examples}),
        "symbols": len(grouped),
        "walk_forward": walk_forward,
        "split_contract": split_contract,
        "failure_diagnosis": diagnosis,
        "pattern_summary": {
            "qualification": CONFIRMED_THRESHOLDS,
            "forming": FORMING_THRESHOLDS,
            "daily_candidate_cap": MAX_DAILY_CANDIDATES,
            "selection_policy": "qualified_only_and_may_abstain",
        },
        "promotion_gate": {
            "status": "eligible_for_manual_review" if promotion_ready else "shadow_only",
            "checks": gate_checks,
            "notice": (
                "The v2 rule was specified after inspecting the frozen v1 test. Its old-window result is "
                "retrospective diagnosis only; promotion requires a new unseen forward window."
            ),
        },
        "current_scores": [
            {**item, "signal_date": str(item["signal_date"])} for item in current_scores
        ],
    }
    parameters = {
        "model_version": MODEL_VERSION,
        "workflow": "qlib_aligned_causal_pattern_challenger",
        "label": {
            "signal_at": "T close", "entry_at": "T+1 open",
            "horizon_trading_days": HORIZON_DAYS,
            "positive": "MFE>=10%, terminal_return>=5%, MAE>=-6%",
        },
        "feature_lookback_trading_days": LOOKBACK_DAYS,
        "feature_keys": list(FEATURE_KEYS),
        "qualification": CONFIRMED_THRESHOLDS,
        "forming": FORMING_THRESHOLDS,
        "selection": f"qualified_only_max_{MAX_DAILY_CANDIDATES}_per_day_may_abstain",
        "round_trip_cost_bps_per_side": 18,
        "live_effect": "shadow_evidence_only",
        "alert_eligible": False,
        "test_reuse_policy": "diagnostic_only_after_v1_test_was_observed",
    }
    return {
        "status": "completed",
        "strategy_key": STRATEGY_KEY,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "parameters": parameters,
        "metrics": metrics,
        "equity_curve": _non_overlapping_curve(selections["test"]),
        "trades": trades,
    }


def run_watchlist_main_wave_v2_research(connection: Any, end_date: date | None = None) -> dict[str, Any]:
    latest = connection.execute(
        """SELECT max(b.trading_date) AS latest FROM quant.canonical_bars_daily b
             JOIN quant.intraday_watchlists w ON w.symbol=b.symbol AND w.enabled"""
    ).fetchone()
    selected_end = (
        min(end_date, latest["latest"]) if end_date and latest and latest["latest"]
        else latest["latest"] if latest else None
    )
    if selected_end is None:
        return {
            "status": "insufficient_history", "strategy_key": STRATEGY_KEY,
            "metrics": {"reason": "watchlist_has_no_daily_bars"}, "parameters": {},
            "equity_curve": [], "trades": [],
        }
    start_date = selected_end - timedelta(days=365)
    rows = connection.execute(
        """SELECT b.symbol,i.name,b.trading_date,b.open,b.high,b.low,b.close,b.volume,b.amount,b.adj_factor,
                  b.is_suspended,b.limit_up,b.limit_down
             FROM quant.canonical_bars_daily b
             JOIN quant.intraday_watchlists w ON w.symbol=b.symbol AND w.enabled
             LEFT JOIN quant.instruments i ON i.symbol=b.symbol
            WHERE b.trading_date BETWEEN %s AND %s
              AND b.quality_status IN ('fresh','partial')
            ORDER BY b.symbol,b.trading_date""",
        (start_date, selected_end),
    ).fetchall()
    return research_from_rows_v2([dict(row) for row in rows], start_date, selected_end)


def latest_shadow_priors_v2(connection: Any) -> dict[str, dict[str, Any]]:
    row = connection.execute(
        """SELECT metrics,parameters,created_at FROM quant.strategy_experiments
            WHERE strategy_key=%s AND status='completed' ORDER BY created_at DESC LIMIT 1""",
        (STRATEGY_KEY,),
    ).fetchone()
    if not row:
        return {}
    parameters, metrics = dict(row["parameters"] or {}), dict(row["metrics"] or {})
    priors = {}
    for item in metrics.get("current_scores") or []:
        symbol = str(item.get("symbol") or "")
        if symbol:
            priors[symbol] = {
                **item,
                "model_version": parameters.get("model_version"),
                "live_effect": parameters.get("live_effect"),
                "trained_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
    return priors


def main_wave_v2_shadow_signal(
    watch: dict[str, Any], quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Require a daily pattern plus causal intraday price/volume confirmation."""
    if not quote or not prior or prior.get("state") not in {"shadow_confirmed", "shadow_forming"}:
        return None
    forming = prior.get("state") == "shadow_forming"
    price_change = _finite(quote.get("pct_change"))
    quote_volume = _finite(quote.get("volume_ratio")) or 0.0
    flow = _finite(quote.get("main_net_inflow")) or 0.0
    return_3m = _finite((minute_features or {}).get("return_3m_pct"))
    minute_volume = _finite((minute_features or {}).get("minute_volume_multiple"))
    above_vwap = _finite((minute_features or {}).get("above_vwap_pct"))
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    minimum_change = 1.0 if forming else 0.5
    minimum_return_3m = 1.0 if forming else 0.5
    minimum_volume = 2.0 if forming else 1.5
    minimum_above_vwap = 0.2 if forming else 0.0
    if (
        price_change is None or not minimum_change <= price_change <= 6.5
        or return_3m is None or return_3m < minimum_return_3m
        or above_vwap is None or above_vwap < minimum_above_vwap
        or max(quote_volume, minute_volume or 0.0) < minimum_volume
        or (flow <= 0 and confirming_peers < 2)
    ):
        return None
    symbol = str(watch["symbol"])
    return {
        "signal_key": f"{symbol}:watch:main_wave_shadow_v2",
        "signal_type": "watch", "severity": "info",
        "score": round(float(prior["model_score"]) * 100, 2),
        "hard": False, "shadow_only": True,
        "conditions": {
            "setup": "daily_main_wave_pattern_plus_intraday_confirmation_v2",
            "daily_pattern": prior,
            "pct_change": price_change, "quote_volume_ratio": quote_volume,
            "main_net_inflow": flow,
            "minute_features": minute_features or {"status": "not_available"},
            "peer_context": peer_context or {"status": "not_available"},
        },
        "risk_flags": [
            "shadow_challenger_not_promoted", "reused_test_window_diagnostic_only",
            "watchlist_selection_bias", "no_feishu_alert", "manual_review_required",
            "no_automatic_order",
        ],
    }


__all__ = [
    "MODEL_VERSION", "STRATEGY_KEY", "evaluate_pattern_split", "latest_shadow_priors_v2",
    "main_wave_pattern", "main_wave_v2_shadow_signal", "research_from_rows_v2",
    "run_watchlist_main_wave_v2_research",
]
