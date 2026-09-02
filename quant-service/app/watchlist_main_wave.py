"""Qlib-aligned main-wave onset research for the explicit watchlist.

The implementation deliberately keeps the model small and inspectable.  Qlib's
important contracts are preserved here: chronological segments, processors fit
on the training segment only, a label-boundary embargo, and next-session entry.
The resulting model is a shadow prior.  It cannot create a live alert until the
out-of-sample promotion gate is satisfied and separately approved.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from typing import Any, Iterable

import numpy as np

from .market_rules import LIMIT_TOLERANCE
from .strategy_thresholds import MAX_ENTRY_INTRADAY_GAIN_PCT


MODEL_VERSION = "watchlist-main-wave-logit-v1"
STRATEGY_KEY = "watchlist_main_wave_shadow_v1"
LOOKBACK_DAYS = 60
HORIZON_DAYS = 10
FEATURE_KEYS = (
    "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
    "ma5_gap", "ma20_60_gap", "prior_high_20_gap", "prior_high_60_gap",
    "volume_ratio_20d", "volume_5_20_ratio", "volatility_5d", "volatility_20d",
    "range_5d", "range_20d", "close_location", "amount_ratio_20d",
)
FEATURE_LABELS = {
    "return_1d": "1日动量", "return_3d": "3日动量", "return_5d": "5日动量",
    "return_10d": "10日动量", "return_20d": "20日动量", "return_60d": "60日动量",
    "ma5_gap": "收盘相对5日均线", "ma20_60_gap": "20/60日均线趋势",
    "prior_high_20_gap": "距前20日高点", "prior_high_60_gap": "距前60日高点",
    "volume_ratio_20d": "当日/20日均量", "volume_5_20_ratio": "5日/20日均量",
    "volatility_5d": "5日波动", "volatility_20d": "20日波动",
    "range_5d": "5日日内振幅", "range_20d": "20日日内振幅",
    "close_location": "当日收盘位置", "amount_ratio_20d": "当日/20日均成交额",
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _average(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _feature_row(rows: list[dict[str, Any]], index: int) -> dict[str, float] | None:
    if index < LOOKBACK_DAYS:
        return None
    window = rows[index - LOOKBACK_DAYS:index + 1]
    if len(window) != LOOKBACK_DAYS + 1:
        return None
    close = np.asarray([item["adjusted_close"] for item in window], dtype=float)
    high = np.asarray([item["adjusted_high"] for item in window], dtype=float)
    low = np.asarray([item["adjusted_low"] for item in window], dtype=float)
    volume = np.asarray([item["volume"] for item in window], dtype=float)
    amount = np.asarray([item["amount"] for item in window], dtype=float)
    if (not np.isfinite(close).all() or not np.isfinite(high).all() or not np.isfinite(low).all()
            or not np.isfinite(volume).all() or not np.isfinite(amount).all()
            or np.any(close <= 0) or np.any(volume < 0) or np.any(amount < 0)):
        return None
    daily_returns = np.diff(close) / close[:-1]
    current_range = high[-1] - low[-1]

    def relative(period: int) -> float:
        return float(close[-1] / close[-1 - period] - 1)

    values = {
        "return_1d": relative(1), "return_3d": relative(3), "return_5d": relative(5),
        "return_10d": relative(10), "return_20d": relative(20), "return_60d": relative(60),
        "ma5_gap": float(close[-1] / np.mean(close[-5:]) - 1),
        "ma20_60_gap": float(np.mean(close[-20:]) / np.mean(close[-60:]) - 1),
        # The current session is excluded from both breakout reference highs.
        "prior_high_20_gap": float(close[-1] / np.max(high[-21:-1]) - 1),
        "prior_high_60_gap": float(close[-1] / np.max(high[:-1]) - 1),
        "volume_ratio_20d": float(volume[-1] / np.mean(volume[-20:])) if np.mean(volume[-20:]) else 0.0,
        "volume_5_20_ratio": float(np.mean(volume[-5:]) / np.mean(volume[-20:])) if np.mean(volume[-20:]) else 0.0,
        "volatility_5d": float(np.std(daily_returns[-5:])),
        "volatility_20d": float(np.std(daily_returns[-20:])),
        "range_5d": float(np.mean((high[-5:] - low[-5:]) / close[-5:])),
        "range_20d": float(np.mean((high[-20:] - low[-20:]) / close[-20:])),
        "close_location": float((close[-1] - low[-1]) / current_range) if current_range > 0 else 0.5,
        "amount_ratio_20d": float(amount[-1] / np.mean(amount[-20:])) if np.mean(amount[-20:]) else 0.0,
    }
    return values if all(math.isfinite(value) for value in values.values()) else None


def normalize_bars(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build adjusted research bars without changing raw execution prices."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        symbol = str(raw.get("symbol") or "")
        factor = _finite(raw.get("adj_factor"))
        open_price, high, low, close = (_finite(raw.get(key)) for key in ("open", "high", "low", "close"))
        volume = _finite(raw.get("volume"))
        amount = _finite(raw.get("amount"))
        if (not symbol or factor is None or factor <= 0 or open_price is None or high is None or low is None
                or close is None or min(open_price, high, low, close) <= 0 or volume is None):
            continue
        grouped[symbol].append({
            "symbol": symbol, "name": raw.get("name"), "trading_date": raw["trading_date"],
            "raw_open": open_price, "raw_high": high, "raw_low": low, "raw_close": close,
            "adjusted_open": open_price * factor, "adjusted_high": high * factor,
            "adjusted_low": low * factor, "adjusted_close": close * factor,
            "volume": volume, "amount": amount if amount is not None else close * volume,
            "is_suspended": bool(raw.get("is_suspended")),
            "limit_up": _finite(raw.get("limit_up")), "limit_down": _finite(raw.get("limit_down")),
        })
    for symbol in grouped:
        grouped[symbol].sort(key=lambda item: item["trading_date"])
    return dict(grouped)


def build_examples(grouped: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create causal feature rows and future-path labels.

    A positive label means that after entering at T+1 open, the next ten
    sessions reached +10%, finished at least +5%, and never traded below -6%.
    Those future values are labels only and are never exposed to live scoring.
    """
    examples: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for symbol, bars in grouped.items():
        if len(bars) <= LOOKBACK_DAYS:
            continue
        for index in range(LOOKBACK_DAYS, len(bars)):
            features = _feature_row(bars, index)
            if features is None:
                continue
            base = {
                "symbol": symbol, "name": bars[index].get("name"),
                "signal_date": bars[index]["trading_date"], "features": features,
            }
            if index == len(bars) - 1:
                current.append(base)
            if index + HORIZON_DAYS >= len(bars):
                continue
            entry = bars[index + 1]
            # A next-session limit-up open or suspension is not a fillable entry.
            if entry.get("is_suspended"):
                continue
            entry_limit_up = _finite(entry.get("limit_up"))
            if entry_limit_up is not None and entry["raw_open"] >= entry_limit_up - LIMIT_TOLERANCE:
                continue
            future = bars[index + 1:index + HORIZON_DAYS + 1]
            entry_price = entry["adjusted_open"]
            maximum_favorable = max(item["adjusted_high"] for item in future) / entry_price - 1
            maximum_adverse = min(item["adjusted_low"] for item in future) / entry_price - 1
            terminal_return = future[-1]["adjusted_close"] / entry_price - 1
            label = int(maximum_favorable >= 0.10 and terminal_return >= 0.05 and maximum_adverse >= -0.06)
            examples.append({
                **base, "label": label, "entry_date": entry["trading_date"],
                "exit_date": future[-1]["trading_date"], "entry_price": entry_price,
                "maximum_favorable_excursion": maximum_favorable,
                "maximum_adverse_excursion": maximum_adverse,
                "terminal_return": terminal_return,
            })
    return examples, current


def chronological_splits(examples: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    dates = sorted({item["signal_date"] for item in examples})
    if len(dates) < 90:
        return {"train": [], "validation": [], "test": []}, {
            "method": "chronological_60_20_20_with_label_embargo", "total_dates": len(dates),
            "embargo_trading_days": HORIZON_DAYS, "reason": "fewer_than_90_evaluable_dates",
        }
    train_boundary, validation_boundary = int(len(dates) * 0.60), int(len(dates) * 0.80)
    split_dates = {
        "train": set(dates[:max(0, train_boundary - HORIZON_DAYS)]),
        "validation": set(dates[train_boundary:max(train_boundary, validation_boundary - HORIZON_DAYS)]),
        "test": set(dates[validation_boundary:]),
    }
    splits = {key: [item for item in examples if item["signal_date"] in values] for key, values in split_dates.items()}
    return splits, {
        "method": "chronological_60_20_20_with_label_embargo",
        "total_dates": len(dates), "embargo_trading_days": HORIZON_DAYS,
        "dropped_boundary_dates": len(dates) - sum(len(values) for values in split_dates.values()),
        "ranges": {
            key: {"start": str(min(values)) if values else None, "end": str(max(values)) if values else None,
                  "dates": len(values)} for key, values in split_dates.items()
        },
    }


def _matrix(rows: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.asarray([[item["features"][key] for key in FEATURE_KEYS] for item in rows], dtype=float),
        np.asarray([item["label"] for item in rows], dtype=float),
    )


def fit_logistic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit a deterministic, class-balanced L2 logistic model."""
    x, y = _matrix(rows)
    means, scales = np.mean(x, axis=0), np.std(x, axis=0)
    scales = np.where(scales > 1e-12, scales, 1.0)
    normalized = (x - means) / scales
    design = np.column_stack((np.ones(len(normalized)), normalized))
    weights = np.zeros(design.shape[1], dtype=float)
    positives, negatives = float(np.sum(y)), float(len(y) - np.sum(y))
    if positives <= 0 or negatives <= 0:
        raise ValueError("training segment must contain both positive and negative labels")
    sample_weight = np.where(y == 1, len(y) / (2 * positives), len(y) / (2 * negatives))
    learning_rate, l2 = 0.08, 0.002
    for _ in range(2_500):
        probability = 1 / (1 + np.exp(-np.clip(design @ weights, -30, 30)))
        gradient = design.T @ ((probability - y) * sample_weight) / len(y)
        gradient += np.r_[0.0, weights[1:]] * l2
        weights -= learning_rate * gradient
    return {
        "feature_keys": list(FEATURE_KEYS), "means": means.tolist(), "scales": scales.tolist(),
        "intercept": float(weights[0]), "coefficients": weights[1:].tolist(),
        "fit_rows": len(rows), "positive_rows": int(positives),
        "optimizer": {"name": "deterministic_batch_gradient_descent", "iterations": 2_500,
                      "learning_rate": learning_rate, "l2": l2, "class_balanced": True},
    }


def score_features(features: dict[str, Any], model: dict[str, Any]) -> float | None:
    try:
        values = np.asarray([float(features[key]) for key in model["feature_keys"]], dtype=float)
        means, scales = np.asarray(model["means"], dtype=float), np.asarray(model["scales"], dtype=float)
        coefficients = np.asarray(model["coefficients"], dtype=float)
        logit = float(model["intercept"]) + float(((values - means) / scales) @ coefficients)
    except (KeyError, TypeError, ValueError):
        return None
    return float(1 / (1 + math.exp(-max(-30.0, min(30.0, logit)))))


def _auc(rows: list[dict[str, Any]]) -> float | None:
    positives = [item for item in rows if item["label"] == 1]
    negatives = [item for item in rows if item["label"] == 0]
    if not positives or not negatives:
        return None
    favorable = 0.0
    for positive in positives:
        for negative in negatives:
            favorable += 1.0 if positive["model_score"] > negative["model_score"] else 0.5 if positive["model_score"] == negative["model_score"] else 0.0
    return favorable / (len(positives) * len(negatives))


def _mark_daily_top_quintile(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_date[row["signal_date"]].append(row)
    selected: list[dict[str, Any]] = []
    for values in by_date.values():
        ordered = sorted(values, key=lambda item: (item["model_score"], item["symbol"]), reverse=True)
        keep = max(1, math.ceil(len(ordered) * 0.20))
        selected.extend(ordered[:keep])
    return selected


def evaluate_split(rows: list[dict[str, Any]], model: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scored = [{**item, "model_score": score_features(item["features"], model)} for item in rows]
    scored = [item for item in scored if item["model_score"] is not None]
    selected = _mark_daily_top_quintile(scored)
    base_rate = _average(item["label"] for item in scored)
    precision = _average(item["label"] for item in selected)
    return {
        "rows": len(scored), "dates": len({item["signal_date"] for item in scored}),
        "symbols": len({item["symbol"] for item in scored}), "positive_rows": sum(item["label"] for item in scored),
        "base_rate": base_rate, "roc_auc": _auc(scored), "selected_rows": len(selected),
        "selected_precision": precision, "selected_lift": precision / base_rate if base_rate else None,
        "selected_terminal_return": _average(item["terminal_return"] for item in selected),
        "selected_mfe": _average(item["maximum_favorable_excursion"] for item in selected),
        "selected_mae": _average(item["maximum_adverse_excursion"] for item in selected),
    }, selected


def pattern_summary(train_rows: list[dict[str, Any]], model: dict[str, Any]) -> dict[str, Any]:
    ranked = sorted(zip(FEATURE_KEYS, model["coefficients"]), key=lambda item: abs(float(item[1])), reverse=True)
    positives = [item for item in train_rows if item["label"] == 1]
    negatives = [item for item in train_rows if item["label"] == 0]
    contrasts = []
    for key in FEATURE_KEYS:
        positive_mean = _average(item["features"][key] for item in positives)
        negative_mean = _average(item["features"][key] for item in negatives)
        contrasts.append({"feature": key, "label": FEATURE_LABELS[key], "positive_mean": positive_mean,
                          "negative_mean": negative_mean, "difference": positive_mean - negative_mean})
    return {
        "positive_coefficients": [{"feature": key, "label": FEATURE_LABELS[key], "coefficient": float(value)}
                                  for key, value in ranked if value > 0][:6],
        "negative_coefficients": [{"feature": key, "label": FEATURE_LABELS[key], "coefficient": float(value)}
                                  for key, value in ranked if value < 0][:6],
        "feature_contrasts": sorted(contrasts, key=lambda item: abs(item["difference"]), reverse=True),
        "interpretation_rule": "coefficient direction is conditional on all other standardized features; it is not a standalone trading threshold",
    }


def _current_scores(current: list[dict[str, Any]], model: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [{**item, "model_score": score_features(item["features"], model)} for item in current]
    rows = [item for item in rows if item["model_score"] is not None]
    ordered = sorted(rows, key=lambda item: (item["model_score"], item["symbol"]), reverse=True)
    count = len(ordered)
    for index, item in enumerate(ordered):
        item["rank"] = index + 1
        item["percentile"] = 1.0 - index / max(1, count)
        item["state"] = "shadow_top_quintile" if index < max(1, math.ceil(count * 0.20)) else "shadow_observe"
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


def research_from_rows(rows: Iterable[dict[str, Any]], start_date: date, end_date: date) -> dict[str, Any]:
    grouped = normalize_bars(rows)
    examples, current = build_examples(grouped)
    splits, split_contract = chronological_splits(examples)
    if any(not splits[key] for key in ("train", "validation", "test")):
        return {
            "status": "insufficient_history", "strategy_key": STRATEGY_KEY,
            "start_date": str(start_date), "end_date": str(end_date),
            "metrics": {"sample_rows": len(examples), "split_contract": split_contract},
            "parameters": {"model_version": MODEL_VERSION}, "equity_curve": [], "trades": [],
        }
    evaluation_model = fit_logistic(splits["train"])
    walk_forward: dict[str, Any] = {}
    selections: dict[str, list[dict[str, Any]]] = {}
    for key in ("train", "validation", "test"):
        walk_forward[key], selections[key] = evaluate_split(splits[key], evaluation_model)
    # After the frozen test is measured, a separate shadow model may learn from
    # every now-mature label.  It is never substituted into the test metrics.
    shadow_model = fit_logistic(examples)
    current_scores = _current_scores(current, shadow_model)
    test = walk_forward["test"]
    gate_checks = {
        "test_dates_ge_30": test["dates"] >= 30,
        "test_positives_ge_30": test["positive_rows"] >= 30,
        "test_auc_ge_0_55": (test["roc_auc"] or 0) >= 0.55,
        "test_lift_ge_1_20": (test["selected_lift"] or 0) >= 1.20,
        "test_selected_return_positive": test["selected_terminal_return"] > 0,
        "test_selected_symbols_ge_10": test["symbols"] >= 10,
        # A historical watchlist is selected with hindsight.  One year of this
        # cohort is useful for shadow research, never sufficient for promotion.
        "point_in_time_unbiased_universe": False,
        "three_year_regime_coverage": False,
        "manual_approval": False,
    }
    promotion_ready = all(gate_checks.values())
    trades = []
    for row in sorted(selections["test"], key=lambda item: (item["signal_date"], -item["model_score"]))[:500]:
        trades.append({
            "symbol": row["symbol"], "name": row.get("name"), "signal_date": str(row["signal_date"]),
            "entry_date": str(row["entry_date"]), "exit_date": str(row["exit_date"]),
            "score": row["model_score"], "label": row["label"],
            "terminal_return": row["terminal_return"], "maximum_favorable_excursion": row["maximum_favorable_excursion"],
            "maximum_adverse_excursion": row["maximum_adverse_excursion"],
        })
    metrics = {
        "sample_rows": len(examples), "evaluable_dates": len({item["signal_date"] for item in examples}),
        "symbols": len(grouped), "walk_forward": walk_forward, "split_contract": split_contract,
        "pattern_summary": pattern_summary(splits["train"], evaluation_model),
        "promotion_gate": {"status": "eligible_for_manual_review" if promotion_ready else "shadow_only",
                           "checks": gate_checks,
                           "notice": "Failed checks keep this model out of Feishu alerts and decision scores."},
        "current_scores": [{**item, "signal_date": str(item["signal_date"])} for item in current_scores],
    }
    parameters = {
        "model_version": MODEL_VERSION, "workflow": "qlib_aligned_native_logistic",
        "label": {"signal_at": "T close", "entry_at": "T+1 open", "horizon_trading_days": HORIZON_DAYS,
                  "positive": "MFE>=10%, terminal_return>=5%, MAE>=-6%"},
        "feature_lookback_trading_days": LOOKBACK_DAYS, "feature_keys": list(FEATURE_KEYS),
        "evaluation_model": evaluation_model, "shadow_model": shadow_model,
        "selection": "daily cross-sectional top quintile", "round_trip_cost_bps_per_side": 18,
        "live_effect": "shadow_evidence_only", "alert_eligible": False,
    }
    return {
        "status": "completed", "strategy_key": STRATEGY_KEY,
        "start_date": str(start_date), "end_date": str(end_date),
        "parameters": parameters, "metrics": metrics,
        "equity_curve": _non_overlapping_curve(selections["test"]), "trades": trades,
    }


def run_watchlist_main_wave_research(connection: Any, end_date: date | None = None) -> dict[str, Any]:
    latest = connection.execute(
        """SELECT max(b.trading_date) AS latest FROM quant.canonical_bars_daily b
             JOIN quant.intraday_watchlists w ON w.symbol=b.symbol AND w.enabled"""
    ).fetchone()
    selected_end = min(end_date, latest["latest"]) if end_date and latest and latest["latest"] else (latest["latest"] if latest else None)
    if selected_end is None:
        return {"status": "insufficient_history", "strategy_key": STRATEGY_KEY,
                "metrics": {"reason": "watchlist_has_no_daily_bars"}, "parameters": {}, "equity_curve": [], "trades": []}
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
    return research_from_rows([dict(row) for row in rows], start_date, selected_end)


def latest_shadow_priors(connection: Any) -> dict[str, dict[str, Any]]:
    row = connection.execute(
        """SELECT metrics,parameters,created_at FROM quant.strategy_experiments
            WHERE strategy_key=%s AND status='completed' ORDER BY created_at DESC LIMIT 1""", (STRATEGY_KEY,),
    ).fetchone()
    if not row:
        return {}
    parameters, metrics = dict(row["parameters"] or {}), dict(row["metrics"] or {})
    priors = {}
    for item in metrics.get("current_scores") or []:
        symbol = str(item.get("symbol") or "")
        if symbol:
            priors[symbol] = {**item, "model_version": parameters.get("model_version"),
                              "live_effect": parameters.get("live_effect"),
                              "trained_at": row["created_at"].isoformat() if row["created_at"] else None}
    return priors


def main_wave_shadow_signal(watch: dict[str, Any], quote: dict[str, Any] | None,
                            minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
                            prior: dict[str, Any] | None) -> dict[str, Any] | None:
    """Combine a close-only prior with causal intraday confirmation evidence."""
    if not quote or not prior or prior.get("state") != "shadow_top_quintile":
        return None
    price_change = _finite(quote.get("pct_change"))
    quote_volume = _finite(quote.get("volume_ratio")) or 0.0
    flow = _finite(quote.get("main_net_inflow")) or 0.0
    return_3m = _finite((minute_features or {}).get("return_3m_pct"))
    minute_volume = _finite((minute_features or {}).get("minute_volume_multiple"))
    above_vwap = _finite((minute_features or {}).get("above_vwap_pct"))
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    if (price_change is None or not 0.5 <= price_change <= MAX_ENTRY_INTRADAY_GAIN_PCT or return_3m is None or return_3m < 0.5
            or above_vwap is None or above_vwap < 0
            or max(quote_volume, minute_volume or 0.0) < 1.5
            or (flow <= 0 and confirming_peers < 2)):
        return None
    symbol = str(watch["symbol"])
    return {
        "signal_key": f"{symbol}:watch:main_wave_shadow_v1", "signal_type": "watch",
        "severity": "info", "score": round(float(prior["model_score"]) * 100, 2), "hard": False,
        "shadow_only": True,
        "conditions": {
            "setup": "daily_main_wave_prior_plus_intraday_confirmation", "model_prior": prior,
            "pct_change": price_change, "quote_volume_ratio": quote_volume, "main_net_inflow": flow,
            "minute_features": minute_features or {"status": "not_available"},
            "peer_context": peer_context or {"status": "not_available"},
        },
        "risk_flags": ["shadow_model_not_promoted", "watchlist_selection_bias", "no_feishu_alert",
                       "manual_review_required", "no_automatic_order"],
    }


__all__ = [
    "FEATURE_KEYS", "HORIZON_DAYS", "LOOKBACK_DAYS", "MODEL_VERSION", "STRATEGY_KEY",
    "build_examples", "chronological_splits", "fit_logistic", "latest_shadow_priors",
    "main_wave_shadow_signal", "normalize_bars", "research_from_rows",
    "run_watchlist_main_wave_research", "score_features",
]
