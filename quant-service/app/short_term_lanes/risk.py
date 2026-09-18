"""Unified risk envelope, intentionally independent from signal scoring."""
from __future__ import annotations

MIN_VOLATILITY_BUFFER_PCT = 0.015
VOLATILITY_BUFFER_FACTOR = 0.006


def volatility_buffer_pct(metrics: dict) -> float:
    """The one definition of the structural buffer, as a fraction of price.

    ``short_term_lanes`` and ``trade_discipline`` quote the same structure to the
    same reader, so they must not each carry their own copy of this constant.
    """
    volatility = max(0.0, float(metrics.get("volatility") or 0.0))
    return max(MIN_VOLATILITY_BUFFER_PCT, volatility * VOLATILITY_BUFFER_FACTOR)


def risk_envelope(lane: str, metrics: dict, regime: dict) -> dict:
    close = float(metrics["close"])
    volatility = max(0.0, float(metrics.get("volatility") or 0.0))
    base_cap = 5.0 if lane in {"relay", "event", "reclaim"} else 8.0
    if volatility >= 4.0:
        base_cap = min(base_cap, 4.0)
    budget = float(regime.get("research_budget", 0.0))
    max_position = round(base_cap * budget, 2)
    structure = float(metrics.get("recent_low") or close)
    volatility_buffer = close * volatility_buffer_pct(metrics)
    failure_reference = round(min(structure, close - volatility_buffer), 2)
    return {
        "research_only": True,
        "max_single_position_pct": max_position,
        "max_sector_exposure_pct": round(20.0 * budget, 2),
        "portfolio_risk_budget": budget,
        "failure_reference": failure_reference,
        "failure_rule": "结构位失守后不能快速收复且板块同步走弱；参考线不是自动止损委托",
        "time_stop": "触发后3个交易日仍无相对强度或量能确认则退出本策略观察",
        "trailing_rule": "出现有效盈利后以最近3日结构低点或1.5倍ATR中较紧者跟踪；ATR缺失时不伪造",
        "execution_constraints": ["A股T+1", "涨跌停可能无法成交", "需计佣金、印花税、过户费和滑点"],
        "note": "风险层不参与信号分数，也不能把研究候选升级成买入授权。",
    }


__all__ = ["MIN_VOLATILITY_BUFFER_PCT", "VOLATILITY_BUFFER_FACTOR", "risk_envelope", "volatility_buffer_pct"]
