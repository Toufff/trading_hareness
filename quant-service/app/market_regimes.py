"""Pure cross-sectional market and index regime calculations.

The functions in this module only transform already persisted evidence.  They
do not read the database, call providers, or infer an Elliott-wave count.
Keeping them independent makes live review and later replay share one rule.
"""

from __future__ import annotations

from statistics import median
from typing import Any


TECHNOLOGY_BOARD_TERMS = ("芯片", "半导体", "通信", "元件", "CPO", "AIDC", "数据中心", "光模块", "华为", "AI", "人工智能", "电子")
DEFENSIVE_BOARD_TERMS = ("贵金属", "黄金", "银行", "白酒", "高股息", "猪肉", "养殖", "医药商业", "农业种植", "生态农业", "食品饮料", "中药")
STRATEGY_INDEX_SYMBOLS = ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ")


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def strategy_rank(values: list[float | None]) -> dict[int, float]:
    known = [(index, value) for index, value in enumerate(values) if value is not None]
    if not known:
        return {}
    ordered = sorted(known, key=lambda item: item[1])
    if len(ordered) == 1:
        return {ordered[0][0]: 1.0 if ordered[0][1] > 0 else 0.0}
    return {index: rank / (len(ordered) - 1) for rank, (index, _) in enumerate(ordered)}


def strategy_market_regime(items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    flows = [_number(item.get("net_inflow")) for item in items]
    changes = [_number(item.get("change_pct")) for item in items]
    known_flows = [value for value in flows if value is not None]
    known_changes = sorted(value for value in changes if value is not None)
    if not known_flows:
        return "blocked", {"known_board_flows": 0, "reason": "no usable board-flow values"}
    positive_share = sum(value > 0 for value in known_flows) / len(known_flows)
    median_change = known_changes[len(known_changes) // 2] if known_changes else None
    if positive_share >= 0.58 and (median_change is None or median_change >= 0):
        regime = "risk_on"
    elif positive_share <= 0.42 and (median_change is None or median_change <= 0):
        regime = "risk_off"
    else:
        regime = "neutral"
    return regime, {"known_board_flows": len(known_flows), "positive_flow_share": round(positive_share, 3),
                    "median_board_change_pct": median_change}


def strategy_market_state(items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    base_regime, base_metrics = strategy_market_regime(items)

    def grouped(terms: tuple[str, ...], *, exclude_technology: bool = False) -> tuple[list[str], list[str]]:
        positive, nonpositive = [], []
        for item in items:
            label = str(item.get("label") or "")
            flow = _number(item.get("net_inflow"))
            lowered = label.lower()
            if flow is None or not any(term.lower() in lowered for term in terms):
                continue
            if exclude_technology and any(term.lower() in lowered for term in TECHNOLOGY_BOARD_TERMS):
                continue
            (positive if flow > 0 else nonpositive).append(label)
        return sorted(set(positive)), sorted(set(nonpositive))

    technology_in, technology_out = grouped(TECHNOLOGY_BOARD_TERMS)
    defensive_in, defensive_out = grouped(DEFENSIVE_BOARD_TERMS, exclude_technology=True)
    if len(defensive_in) >= 2 and len(technology_out) >= 2:
        state = "rotation_defensive"
    elif len(technology_in) >= 2 and len(defensive_out) >= 2:
        state = "rotation_technology"
    elif base_regime == "risk_on":
        state = "broad_risk_on"
    elif base_regime == "risk_off":
        state = "broad_risk_off"
    else:
        state = "mixed_or_neutral"
    return state, {**base_metrics, "technology_inflow_boards": technology_in,
                   "technology_outflow_boards": technology_out,
                   "defensive_inflow_boards": defensive_in,
                   "defensive_outflow_boards": defensive_out}


def strategy_index_regime(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("symbol")), []).append(row)
    items: list[dict[str, Any]] = []
    for symbol in STRATEGY_INDEX_SYMBOLS:
        ordered = sorted(grouped.get(symbol, []), key=lambda row: str(row.get("trading_date")))
        if len(ordered) < 15:
            continue
        window = ordered[-30:]
        closes = [_number(row.get("close")) for row in window]
        highs = [_number(row.get("high")) for row in window]
        lows = [_number(row.get("low")) for row in window]
        if any(value is None or value <= 0 for value in closes + highs + lows):
            continue
        close_values = [float(value) for value in closes if value is not None]
        high_value = max(float(value) for value in highs if value is not None)
        low_value = min(float(value) for value in lows if value is not None)
        latest = close_values[-1]
        range_value = high_value - low_value
        retracement = (latest - low_value) / range_value if range_value > 0 else None
        drawdown_from_high = (low_value / high_value - 1) * 100 if high_value > 0 else None
        versus_high = (latest / high_value - 1) * 100 if high_value > 0 else None
        rebound_from_low = (latest / low_value - 1) * 100 if low_value > 0 else None
        returns_5 = (latest / close_values[-6] - 1) * 100 if close_values[-6] > 0 else None
        sma5 = sum(close_values[-5:]) / 5
        sma20 = sum(close_values[-20:]) / 20 if len(close_values) >= 20 else sum(close_values) / len(close_values)
        volumes = [_number(row.get("volume")) for row in window]
        recent = [float(value) for value in volumes[-5:] if value is not None and value > 0]
        prior = [float(value) for value in volumes[-20:-5] if value is not None and value > 0]
        volume_ratio = sum(recent) / len(recent) / (sum(prior) / len(prior)) if recent and prior and sum(prior) else None
        items.append({"symbol": symbol, "trading_date": str(window[-1].get("trading_date")), "close": latest,
                      "period_high": high_value, "period_low": low_value,
                      "drawdown_high_to_low_pct": round(drawdown_from_high, 3) if drawdown_from_high is not None else None,
                      "rebound_from_low_pct": round(rebound_from_low, 3) if rebound_from_low is not None else None,
                      "versus_period_high_pct": round(versus_high, 3) if versus_high is not None else None,
                      "range_retracement": round(retracement, 4) if retracement is not None else None,
                      "return_5_sessions_pct": round(returns_5, 3), "above_sma5": latest >= sma5,
                      "above_sma20": latest >= sma20,
                      "volume_ratio_5_vs_prior15": round(volume_ratio, 4) if volume_ratio is not None else None})
    retracements = [float(item["range_retracement"]) for item in items if item.get("range_retracement") is not None]
    materially_drawn = [item for item in items if float(item.get("drawdown_high_to_low_pct") or 0) <= -8]
    below_high = [item for item in items if float(item.get("versus_period_high_pct") or 0) <= -3]
    rising = [item for item in items if float(item.get("return_5_sessions_pct") or 0) > 0]
    if len(items) < 3:
        state = "insufficient_index_history"
    elif len(materially_drawn) >= 3 and len(below_high) >= 3 and len(rising) >= 3 and 0.25 <= median(retracements) <= 0.80:
        state = "corrective_rebound"
    elif sum(bool(item["above_sma20"]) for item in items) >= 3 and median(retracements) > 0.80:
        state = "trend_recovery"
    elif len(rising) <= 1 and median(retracements) < 0.35:
        state = "weak_or_declining"
    else:
        state = "mixed_transition"
    return {"model_version": "multi-index-corrective-regime-v1", "state": state,
            "index_count": len(items), "median_range_retracement": round(median(retracements), 4) if retracements else None,
            "items": items,
            "interpretation": "B-wave is an analyst scenario label only" if state == "corrective_rebound" else "no Elliott-wave label inferred"}


__all__ = ["DEFENSIVE_BOARD_TERMS", "STRATEGY_INDEX_SYMBOLS", "TECHNOLOGY_BOARD_TERMS", "strategy_index_regime",
           "strategy_market_regime", "strategy_market_state", "strategy_rank"]
