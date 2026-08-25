"""Counter-trend rebound research for the technology watchlist cohort.

This strategy is deliberately separate from the main-wave breakout model.  It
models a decline, panic exhaustion, one-day rebound probe, and confirmed
counter-trend rebound as different states.  Panic is evidence, not an entry.
Only broad, consecutive recovery plus individual reclaim can form a confirmed
shadow candidate.  No state in this module is eligible for automatic trading
or Feishu delivery before the independent-event promotion gate is satisfied.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
import math
from typing import Any, Iterable

from .intraday_decision_context import shrunk_probability
from .strategy_thresholds import MAX_ENTRY_INTRADAY_GAIN_PCT
from .watchlist_main_wave import FEATURE_KEYS, LOOKBACK_DAYS, _feature_row, normalize_bars


MODEL_VERSION = "watchlist-countertrend-rebound-state-v1"
STRATEGY_KEY = "watchlist_countertrend_rebound_shadow_v1"
HORIZON_DAYS = 5
MAX_DAILY_CANDIDATES = 5
ONE_WAY_COST_RATE = 0.0018
TECH_INDUSTRIES = frozenset({"元器件", "半导体", "通信设备", "软件服务", "专用机械", "电气设备"})

STATE_LABELS = {
    "neutral": "常态",
    "decline": "下跌浪/空仓优先",
    "panic": "恐慌耗竭观察",
    "probe": "单日反抽试探",
    "confirmed": "B浪反弹确认",
    "extended": "反弹延伸/不追高",
    "invalid": "数据不足",
}


def _average(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _median(values: Iterable[float]) -> float:
    rows = sorted(values)
    if not rows:
        return 0.0
    middle = len(rows) // 2
    return rows[middle] if len(rows) % 2 else (rows[middle - 1] + rows[middle]) / 2


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


def rebound_state(
    features: dict[str, Any], market: dict[str, Any] | None, cohort: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a causal state using only information available at T close."""
    if not market or not cohort:
        return {"state": "invalid", "label": STATE_LABELS["invalid"], "strength": 0.0}
    values = {key: _finite(features.get(key)) for key in FEATURE_KEYS}
    required_context = {
        "market_breadth": _finite(market.get("breadth")),
        "market_median_change_pct": _finite(market.get("median_change_pct")),
        "previous_market_breadth": _finite(cohort.get("previous_market_breadth")),
        "cohort_above_ma5_ratio": _finite(cohort.get("above_ma5_ratio")),
        "cohort_median_return_3d": _finite(cohort.get("median_return_3d")),
        "cohort_median_return_20d": _finite(cohort.get("median_return_20d")),
    }
    if any(value is None for value in values.values()) or any(value is None for value in required_context.values()):
        return {"state": "invalid", "label": STATE_LABELS["invalid"], "strength": 0.0}
    f = {key: float(value) for key, value in values.items() if value is not None}
    c = {key: float(value) for key, value in required_context.items() if value is not None}
    decline = c["cohort_median_return_20d"] <= -0.10 and f["return_20d"] < 0
    panic = (
        decline
        and c["market_breadth"] <= 0.20
        and c["market_median_change_pct"] <= -2.50
        and f["prior_high_20_gap"] <= -0.15
        and (f["close_location"] >= 0.55 or f["return_1d"] >= 0)
    )
    confirmed = (
        f["prior_high_20_gap"] <= -0.10
        and f["return_3d"] >= 0.04
        and f["ma5_gap"] >= 0
        and f["close_location"] >= 0.55
        and c["market_breadth"] >= 0.55
        and c["previous_market_breadth"] >= 0.55
        and c["cohort_above_ma5_ratio"] >= 0.60
        and c["cohort_median_return_3d"] >= 0.02
    )
    extended = confirmed and (f["return_5d"] >= 0.18 or f["return_3d"] >= 0.20)
    probe = (
        decline
        and f["prior_high_20_gap"] <= -0.10
        and f["return_1d"] >= 0.03
        and f["close_location"] >= 0.70
        and c["market_breadth"] >= 0.55
    )
    state = (
        "extended" if extended else "confirmed" if confirmed else "panic" if panic
        else "probe" if probe else "decline" if decline else "neutral"
    )
    components = {
        "oversold_depth": _clip(-f["prior_high_20_gap"], 0.10, 0.50),
        "three_day_reclaim": _clip(f["return_3d"], 0.0, 0.20),
        "close_location": _clip(f["close_location"], 0.30, 1.0),
        "market_breadth": _clip(c["market_breadth"], 0.20, 0.80),
        "cohort_repair": _clip(c["cohort_above_ma5_ratio"], 0.20, 1.0),
    }
    strength = _average(components.values())
    return {
        "state": state,
        "label": STATE_LABELS[state],
        "strength": round(strength, 8),
        "components": components,
        "context": c,
        "discipline": (
            "panic_is_observation_only" if state == "panic" else
            "single_day_rebound_is_not_confirmation" if state == "probe" else
            "do_not_chase_extended_rebound" if state == "extended" else
            "confirmed_requires_consecutive_market_breadth_and_cohort_repair" if state == "confirmed" else
            "prefer_cash_until_repair" if state == "decline" else "no_action"
        ),
    }


def _market_context(rows: Iterable[dict[str, Any]]) -> tuple[dict[date, dict[str, Any]], dict[date, date | None]]:
    market: dict[date, dict[str, Any]] = {}
    for raw in rows:
        trading_date = raw["trading_date"]
        stock_count = int(raw.get("stock_count") or 0)
        market[trading_date] = {
            **raw,
            "breadth": int(raw.get("advancers") or 0) / stock_count if stock_count else None,
        }
    dates = sorted(market)
    previous = {trading_date: dates[index - 1] if index else None for index, trading_date in enumerate(dates)}
    return market, previous


def build_rebound_examples(
    raw_bars: Iterable[dict[str, Any]], market_rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grouped = normalize_bars(raw_bars)
    market, previous_market_date = _market_context(market_rows)
    feature_rows: list[dict[str, Any]] = []
    for symbol, bars in grouped.items():
        for index in range(LOOKBACK_DAYS, len(bars)):
            features = _feature_row(bars, index)
            if features is None:
                continue
            feature_rows.append({
                "symbol": symbol, "name": bars[index].get("name"),
                "signal_date": bars[index]["trading_date"], "features": features,
                "bars": bars, "bar_index": index,
            })
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in feature_rows:
        by_date[row["signal_date"]].append(row)
    cohort_context: dict[date, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for trading_date, values in sorted(by_date.items()):
        previous_date = previous_market_date.get(trading_date)
        previous_breadth = market.get(previous_date, {}).get("breadth") if previous_date else None
        context = {
            "symbol_count": len(values),
            "above_ma5_ratio": _average(int(item["features"]["ma5_gap"] >= 0) for item in values),
            "median_return_3d": _median(item["features"]["return_3d"] for item in values),
            "median_return_20d": _median(item["features"]["return_20d"] for item in values),
            "median_prior_high_20_gap": _median(item["features"]["prior_high_20_gap"] for item in values),
            "previous_market_breadth": previous_breadth,
        }
        cohort_context[trading_date] = context
        market_row = market.get(trading_date)
        if market_row:
            broad_confirmed = (
                (market_row.get("breadth") or 0) >= 0.55
                and (previous_breadth or 0) >= 0.55
                and context["above_ma5_ratio"] >= 0.60
                and context["median_return_3d"] >= 0.02
            )
            regime = (
                "rebound_confirmed" if broad_confirmed else
                "panic" if (market_row.get("breadth") or 1) <= 0.20 else
                "decline" if context["median_return_20d"] <= -0.10 else "neutral"
            )
            timeline.append({
                "trading_date": str(trading_date), "regime": regime,
                "market_breadth": market_row.get("breadth"),
                "previous_market_breadth": previous_breadth,
                **context,
            })
    examples: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for row in feature_rows:
        trading_date = row["signal_date"]
        assessment = rebound_state(row["features"], market.get(trading_date), cohort_context.get(trading_date))
        base = {
            "symbol": row["symbol"], "name": row.get("name"), "signal_date": trading_date,
            "features": row["features"], "pattern": assessment,
            "model_score": assessment["strength"],
        }
        bars, index = row["bars"], row["bar_index"]
        if index == len(bars) - 1:
            current.append(base)
        if index + HORIZON_DAYS >= len(bars):
            continue
        entry = bars[index + 1]
        # A next-session limit-up open or suspension is not a fillable entry.
        if entry.get("is_suspended"):
            continue
        limit_up = _finite(entry.get("limit_up"))
        if limit_up is not None and entry["raw_open"] >= limit_up * 0.999:
            continue
        future = bars[index + 1:index + HORIZON_DAYS + 1]
        entry_price = entry["adjusted_open"]
        maximum_favorable = max(item["adjusted_high"] for item in future) / entry_price - 1
        maximum_adverse = min(item["adjusted_low"] for item in future) / entry_price - 1
        terminal_return = future[-1]["adjusted_close"] / entry_price - 1
        net_terminal_return = (1 + terminal_return) * (1 - ONE_WAY_COST_RATE) ** 2 - 1
        label = int(maximum_favorable >= 0.08 and terminal_return >= 0.03 and maximum_adverse >= -0.06)
        examples.append({
            **base, "label": label, "entry_date": entry["trading_date"],
            "exit_date": future[-1]["trading_date"], "entry_price": entry_price,
            "maximum_favorable_excursion": maximum_favorable,
            "maximum_adverse_excursion": maximum_adverse,
            "terminal_return": terminal_return, "net_terminal_return": net_terminal_return,
        })
    return examples, current, timeline


def chronological_rebound_splits(examples: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    dates = sorted({item["signal_date"] for item in examples})
    if len(dates) < 90:
        return {"train": [], "validation": [], "test": []}, {
            "method": "chronological_60_20_20_with_5d_embargo", "total_dates": len(dates),
            "reason": "fewer_than_90_evaluable_dates",
        }
    train_boundary, validation_boundary = int(len(dates) * 0.60), int(len(dates) * 0.80)
    split_dates = {
        "train": set(dates[:max(0, train_boundary - HORIZON_DAYS)]),
        "validation": set(dates[train_boundary:max(train_boundary, validation_boundary - HORIZON_DAYS)]),
        "test": set(dates[validation_boundary:]),
    }
    return (
        {key: [item for item in examples if item["signal_date"] in values] for key, values in split_dates.items()},
        {
            "method": "chronological_60_20_20_with_5d_embargo", "total_dates": len(dates),
            "embargo_trading_days": HORIZON_DAYS,
            "ranges": {
                key: {"start": str(min(values)) if values else None, "end": str(max(values)) if values else None,
                      "dates": len(values)} for key, values in split_dates.items()
            },
        },
    )


def _select_confirmed(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["pattern"]["state"] == "confirmed":
            by_date[row["signal_date"]].append(row)
    selected: list[dict[str, Any]] = []
    for values in by_date.values():
        selected.extend(sorted(values, key=lambda item: (
            item["model_score"], item["symbol"],
        ), reverse=True)[:MAX_DAILY_CANDIDATES])
    return selected


def evaluate_rebound_split(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selected = _select_confirmed(rows)
    panic = [item for item in rows if item["pattern"]["state"] == "panic"]
    base_rate = _average(item["label"] for item in rows)
    precision = _average(item["label"] for item in selected)
    selected_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for item in selected:
        selected_by_date[item["signal_date"]].append(item)
    selected_date_positive_rate = _average(
        _average(item["label"] for item in items) for items in selected_by_date.values()
    )
    return {
        "rows": len(rows), "dates": len({item["signal_date"] for item in rows}),
        "symbols": len({item["symbol"] for item in rows}), "base_rate": base_rate,
        "positive_rows": sum(item["label"] for item in rows),
        "selected_rows": len(selected), "selected_dates": len({item["signal_date"] for item in selected}),
        "selected_symbols": len({item["symbol"] for item in selected}),
        "selected_positive_rows": sum(item["label"] for item in selected),
        "selected_precision": precision, "selected_lift": precision / base_rate if base_rate else None,
        "selected_date_positive_rate": selected_date_positive_rate,
        "selected_terminal_return": _average(item["terminal_return"] for item in selected),
        "selected_net_terminal_return": _average(item.get("net_terminal_return", item["terminal_return"]) for item in selected),
        "selected_mfe": _average(item["maximum_favorable_excursion"] for item in selected),
        "selected_mae": _average(item["maximum_adverse_excursion"] for item in selected),
        "panic_rows": len(panic), "panic_positive_rate": _average(item["label"] for item in panic),
        "panic_mfe": _average(item["maximum_favorable_excursion"] for item in panic),
        "panic_mae": _average(item["maximum_adverse_excursion"] for item in panic),
    }, selected


def _current_scores(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state_order = {"confirmed": 0, "panic": 1, "probe": 2, "extended": 3, "decline": 4, "neutral": 5, "invalid": 6}
    ordered = sorted(rows, key=lambda item: (
        -state_order.get(item["pattern"]["state"], 9), item["model_score"], item["symbol"],
    ), reverse=True)
    for index, item in enumerate(ordered):
        item["rank"] = index + 1
        item["percentile"] = 1.0 - index / max(1, len(ordered))
        item["state"] = f"shadow_{item['pattern']['state']}"
    return ordered


def research_from_rows(
    raw_bars: Iterable[dict[str, Any]], market_rows: Iterable[dict[str, Any]],
    start_date: date, end_date: date,
) -> dict[str, Any]:
    examples, current, timeline = build_rebound_examples(raw_bars, market_rows)
    splits, split_contract = chronological_rebound_splits(examples)
    if any(not splits[key] for key in ("train", "validation", "test")):
        return {
            "status": "insufficient_history", "strategy_key": STRATEGY_KEY,
            "start_date": str(start_date), "end_date": str(end_date),
            "parameters": {"model_version": MODEL_VERSION},
            "metrics": {"sample_rows": len(examples), "split_contract": split_contract},
            "equity_curve": [], "trades": [],
        }
    walk_forward, selections = {}, {}
    for key in ("train", "validation", "test"):
        walk_forward[key], selections[key] = evaluate_rebound_split(splits[key])
    test = walk_forward["test"]
    gate_checks = {
        "independent_selected_dates_ge_30": test["selected_dates"] >= 30,
        "selected_positive_rows_ge_50": test["selected_positive_rows"] >= 50,
        "test_lift_ge_1_20": (test["selected_lift"] or 0) >= 1.20,
        "test_selected_net_return_positive": test["selected_net_terminal_return"] > 0,
        "test_selected_mae_above_minus_8pct": test["selected_mae"] >= -0.08,
        "fresh_unseen_forward_window": False,
        "point_in_time_unbiased_universe": False,
        "manual_approval": False,
    }
    current_scores = _current_scores(current)
    trades = [
        {
            "symbol": item["symbol"], "name": item.get("name"),
            "signal_date": str(item["signal_date"]), "entry_date": str(item["entry_date"]),
            "exit_date": str(item["exit_date"]), "score": item["model_score"],
            "label": item["label"], "pattern": item["pattern"],
            "terminal_return": item["terminal_return"],
            "maximum_favorable_excursion": item["maximum_favorable_excursion"],
            "maximum_adverse_excursion": item["maximum_adverse_excursion"],
            "net_terminal_return": item["net_terminal_return"],
        }
        for item in sorted(selections["test"], key=lambda row: (row["signal_date"], -row["model_score"]))
    ]
    return {
        "status": "completed", "strategy_key": STRATEGY_KEY,
        "start_date": str(start_date), "end_date": str(end_date),
        "parameters": {
            "model_version": MODEL_VERSION,
            "workflow": "causal_countertrend_state_machine",
            "technology_industries": sorted(TECH_INDUSTRIES),
            "feature_lookback_trading_days": LOOKBACK_DAYS,
            "label": {
                "signal_at": "T close", "entry_at": "T+1 open", "horizon_trading_days": HORIZON_DAYS,
                "positive": "MFE>=8%, terminal_return>=3%, MAE>=-6%",
            },
            "selection": f"confirmed_only_max_{MAX_DAILY_CANDIDATES}_per_day",
            "one_way_cost_bps": int(ONE_WAY_COST_RATE * 10_000),
            "panic_policy": "observation_only_never_direct_entry",
            "live_effect": "explicit_watchlist_research_alert_only", "alert_eligible": True,
            "probability_contract": "shrunk_research_probability_with_effective_trading_days",
            "test_reuse_policy": "diagnostic_only_after_july_august_were_observed",
        },
        "metrics": {
            "sample_rows": len(examples), "symbols": len({item["symbol"] for item in examples}),
            "walk_forward": walk_forward, "split_contract": split_contract,
            "regime_timeline": timeline[-80:],
            "current_scores": [
                {**item, "signal_date": str(item["signal_date"])} for item in current_scores
            ],
            "promotion_gate": {
                "status": "eligible_for_manual_review" if all(gate_checks.values()) else "shadow_only",
                "checks": gate_checks,
                "notice": (
                    "July-August informed this state machine. Historical precision is diagnostic only; "
                    "promotion requires at least 30 new independent confirmation dates."
                ),
            },
        },
        "equity_curve": [], "trades": trades,
    }


def run_countertrend_rebound_research(connection: Any, end_date: date | None = None) -> dict[str, Any]:
    latest = connection.execute(
        """SELECT max(b.trading_date) AS latest FROM quant.canonical_bars_daily b
             JOIN quant.intraday_watchlists w ON w.symbol=b.symbol AND w.enabled
             JOIN quant.instruments i ON i.symbol=b.symbol
            WHERE i.industry=ANY(%s)""", (list(TECH_INDUSTRIES),),
    ).fetchone()
    selected_end = (
        min(end_date, latest["latest"]) if end_date and latest and latest["latest"]
        else latest["latest"] if latest else None
    )
    if selected_end is None:
        return {
            "status": "insufficient_history", "strategy_key": STRATEGY_KEY,
            "parameters": {}, "metrics": {"reason": "technology_watchlist_has_no_daily_bars"},
            "equity_curve": [], "trades": [],
        }
    start_date = selected_end - timedelta(days=365)
    raw_bars = connection.execute(
        """SELECT b.symbol,i.name,b.trading_date,b.open,b.high,b.low,b.close,b.volume,b.amount,b.adj_factor,
                  b.is_suspended,b.limit_up,b.limit_down
             FROM quant.canonical_bars_daily b
             JOIN quant.intraday_watchlists w ON w.symbol=b.symbol AND w.enabled
             JOIN quant.instruments i ON i.symbol=b.symbol
            WHERE b.trading_date BETWEEN %s AND %s
              AND b.quality_status IN ('fresh','partial') AND i.industry=ANY(%s)
            ORDER BY b.symbol,b.trading_date""",
        (start_date, selected_end, list(TECH_INDUSTRIES)),
    ).fetchall()
    market_rows = connection.execute(
        """SELECT trading_date,stock_count,advancers,decliners,unchanged,median_change_pct,
                  mean_change_pct,total_amount_kcny,total_volume_lots,available_at
             FROM quant.daily_market_aggregates WHERE trading_date BETWEEN %s AND %s ORDER BY trading_date""",
        (start_date, selected_end),
    ).fetchall()
    return research_from_rows([dict(row) for row in raw_bars], [dict(row) for row in market_rows], start_date, selected_end)


def latest_rebound_priors(connection: Any) -> dict[str, dict[str, Any]]:
    row = connection.execute(
        """SELECT metrics,parameters,created_at FROM quant.strategy_experiments
            WHERE strategy_key=%s AND status='completed' ORDER BY created_at DESC LIMIT 1""",
        (STRATEGY_KEY,),
    ).fetchone()
    if not row:
        return {}
    parameters, metrics = dict(row["parameters"] or {}), dict(row["metrics"] or {})
    test = ((metrics.get("walk_forward") or {}).get("test") or {})
    selected_date_rate = test.get("selected_date_positive_rate")
    if selected_date_rate is None:
        selected_date_rate = test.get("selected_precision")
    base_rate = _finite(test.get("base_rate"))
    probability = shrunk_probability(
        raw_positive_rate=_finite(selected_date_rate),
        sample_rows=int(test.get("selected_rows") or 0),
        independent_days=int(test.get("selected_dates") or 0),
        average_directional_return=_finite(test.get("selected_net_terminal_return")),
        horizon="5d", source="countertrend_rebound_july_august_diagnostic",
        prior_rate=base_rate if base_rate is not None else 0.50,
        outcome_definition="MFE>=8%, terminal>=3%, MAE>=-6% after T+1 open",
    )
    return {
        str(item["symbol"]): {
            **item, "model_version": parameters.get("model_version"),
            "live_effect": parameters.get("live_effect"),
            "research_probability": probability,
            "trained_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for item in metrics.get("current_scores") or [] if item.get("symbol")
    }


def countertrend_rebound_realtime_signal(
    watch: dict[str, Any], quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Alert an explicit watch only after close-state and intraday confirmation."""
    if (
        not quote or not prior or prior.get("state") != "shadow_confirmed"
        or not bool(watch.get("alert_on_entry")) or watch.get("entry_price") is not None
    ):
        return None
    price_change = _finite(quote.get("pct_change"))
    quote_volume = _finite(quote.get("volume_ratio")) or 0.0
    flow = _finite(quote.get("main_net_inflow")) or 0.0
    return_3m = _finite((minute_features or {}).get("return_3m_pct"))
    minute_volume = _finite((minute_features or {}).get("minute_volume_multiple"))
    above_vwap = _finite((minute_features or {}).get("above_vwap_pct"))
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    if (
        price_change is None or not 0.5 <= price_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
        or return_3m is None or return_3m < 0.5
        or above_vwap is None or above_vwap < 0
        or max(quote_volume, minute_volume or 0.0) < 1.5
        or (flow <= 0 and confirming_peers < 2)
    ):
        return None
    symbol = str(watch["symbol"])
    return {
        "signal_key": f"{symbol}:entry:countertrend_rebound_v1",
        "signal_type": "entry", "severity": "warning",
        "score": round(float(prior["model_score"]) * 100, 2),
        "hard": False, "strategy_version": MODEL_VERSION,
        "independent_confirmation": confirming_peers >= 2,
        "conditions": {
            "setup": "countertrend_rebound_confirmed_plus_intraday_acceptance",
            "daily_rebound_state": prior,
            "price": _finite(quote.get("price")),
            "pct_change": price_change, "volume_ratio": quote_volume,
            "turnover_rate": _finite(quote.get("turnover_rate")),
            "main_net_inflow": flow,
            "minute_features": minute_features or {"status": "not_available"},
            "peer_context": peer_context or {"status": "not_available"},
            "research_probability": prior.get("research_probability"),
        },
        "risk_flags": [
            "countertrend_not_main_wave", "research_alert_only_not_strategy_promotion",
            "panic_stage_is_not_entry", "reused_test_window_diagnostic_only",
            "low_confidence_probability", "watchlist_selection_bias", "manual_review_required",
            "no_automatic_order",
        ],
    }


def countertrend_rebound_failure_reduce_signal(
    watch: dict[str, Any], quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Flag a failed intraday rebound for an existing position.

    This is deliberately a *reduce* review, not an automatic stop.  It needs
    a loss of VWAP acceptance and negative short-horizon momentum together
    with either negative flow or a loss of exact-peer breadth.  The condition
    avoids treating a normal one-minute pullback as an exit signal, while
    keeping T+1/can-sell enforcement in the common live-policy gate.
    """
    # This is a lifecycle transition for a *confirmed counter-trend rebound*,
    # not a generic position stop.  Without the prior daily state, every
    # losing enabled position could be mislabelled as a B-wave failure merely
    # because it traded below VWAP for a few minutes.  Generic risk rules keep
    # their own signal keys and remain responsible for those positions.
    prior_state = str((prior or {}).get("state") or "")
    if prior_state not in {"shadow_confirmed", "shadow_extended"}:
        return None
    entry_price = _finite(watch.get("entry_price"))
    if not quote or entry_price is None or entry_price <= 0 or not bool(watch.get("alert_on_exit")):
        return None
    price = _finite(quote.get("price"))
    return_3m = _finite((minute_features or {}).get("return_3m_pct"))
    above_vwap = _finite((minute_features or {}).get("above_vwap_pct"))
    flow = _finite(quote.get("main_net_inflow"))
    available_peers = int((peer_context or {}).get("available_peer_count") or 0)
    confirming_peers = int((peer_context or {}).get("confirming_peer_count") or 0)
    if price is None or return_3m is None or above_vwap is None:
        return None
    return_since_entry = (price / entry_price - 1) * 100
    peer_confirmation_lost = available_peers >= 2 and confirming_peers == 0
    vwap_acceptance_lost = above_vwap <= -0.15 and return_3m <= -0.50
    cost_risk_lost = return_since_entry <= -2.5 and (above_vwap < 0 or return_3m < 0)
    if not ((vwap_acceptance_lost and (flow is None or flow <= 0 or peer_confirmation_lost)) or cost_risk_lost):
        return None
    symbol = str(watch["symbol"])
    return {
        "signal_key": f"{symbol}:reduce:countertrend_rebound_failure_v1",
        "signal_type": "reduce", "severity": "warning",
        "score": round(float(prior.get("model_score") or 0.0) * 100, 2),
        "hard": False, "strategy_version": MODEL_VERSION,
        "independent_confirmation": peer_confirmation_lost,
        "conditions": {
            "setup": "countertrend_rebound_intraday_acceptance_failure",
            "daily_rebound_state": prior,
            "entry_price": entry_price, "price": price,
            "return_since_entry_pct": round(return_since_entry, 4),
            "pct_change": _finite(quote.get("pct_change")),
            "volume_ratio": _finite(quote.get("volume_ratio")),
            "turnover_rate": _finite(quote.get("turnover_rate")),
            "main_net_inflow": flow,
            "minute_features": minute_features or {"status": "not_available"},
            "peer_context": peer_context or {"status": "not_available"},
            "vwap_acceptance_lost": vwap_acceptance_lost,
            "peer_confirmation_lost": peer_confirmation_lost,
            "cost_risk_lost": cost_risk_lost,
        },
        "risk_flags": [
            "countertrend_rebound_acceptance_failed", "manual_review_required",
            "no_automatic_order", "t_plus_one_and_limit_down_policy_checked_separately",
        ],
    }


def countertrend_rebound_shadow_signal(
    watch: dict[str, Any], quote: dict[str, Any] | None,
    minute_features: dict[str, Any] | None, peer_context: dict[str, Any] | None,
    prior: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compatibility alias for callers deployed before the alert-only upgrade."""
    return countertrend_rebound_realtime_signal(watch, quote, minute_features, peer_context, prior)


__all__ = [
    "HORIZON_DAYS", "MODEL_VERSION", "STRATEGY_KEY", "TECH_INDUSTRIES",
    "build_rebound_examples", "chronological_rebound_splits",
    "countertrend_rebound_failure_reduce_signal", "countertrend_rebound_realtime_signal",
    "countertrend_rebound_shadow_signal", "evaluate_rebound_split",
    "latest_rebound_priors", "rebound_state", "research_from_rows",
    "run_countertrend_rebound_research",
]
