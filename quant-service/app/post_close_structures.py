"""Side-effect-free post-close consolidation and early-expansion structures.

These are descriptive research screens, never order rules.  Keeping them free
of database, provider, and wall-clock dependencies lets P2 replay and P3
validation invoke exactly the same implementation as the live post-close path.
"""

from __future__ import annotations

import math
from typing import Any


POST_CLOSE_STRATEGY_MODEL_VERSION = "post-close-base-start-v1"


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def daily_base_structure(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe a 30-session contraction base without predicting a breakout."""
    if len(bars) < 30:
        return {"status": "insufficient_history", "bar_count": len(bars), "score": 0}
    window = bars[-30:]
    closes = [_number(row.get("close")) for row in window]
    if any(value is None or value <= 0 for value in closes):
        return {"status": "invalid_prices", "bar_count": len(window), "score": 0}
    close_values = [float(value) for value in closes if value is not None]
    highs = [float(_number(row.get("high")) or close_values[index]) for index, row in enumerate(window)]
    lows = [float(_number(row.get("low")) or close_values[index]) for index, row in enumerate(window)]
    volumes = [_number(row.get("volume")) for row in window]

    def realized_volatility(values: list[float]) -> float | None:
        returns = [values[index] / values[index - 1] - 1 for index in range(1, len(values)) if values[index - 1] > 0]
        return math.sqrt(_mean([item * item for item in returns])) if returns else None

    old_volatility = realized_volatility(close_values[-20:-10])
    recent_volatility = realized_volatility(close_values[-10:])
    base_low, base_high = min(lows[-20:]), max(highs[-20:])
    base_range_pct = (base_high / base_low - 1) * 100 if base_low > 0 else None
    recent_low, recent_high = min(lows[-5:]), max(highs[-5:])
    recent_range_pct = (recent_high / recent_low - 1) * 100 if recent_low > 0 else None
    resistance = max(highs[-20:-1])
    close_to_resistance_pct = (resistance / close_values[-1] - 1) * 100 if close_values[-1] > 0 else None
    support = min(lows[-20:])
    support_tests = sum(1 for low in lows[-20:] if low <= support * 1.04)
    recent_volumes = [float(value) for value in volumes[-5:] if value is not None and value > 0]
    prior_volumes = [float(value) for value in volumes[-20:-5] if value is not None and value > 0]
    volume_dry_up_ratio = _mean(recent_volumes) / _mean(prior_volumes) if recent_volumes and prior_volumes and _mean(prior_volumes) else None
    sma20 = _mean(close_values[-20:])
    components = {
        "horizontal_base": base_range_pct is not None and 4.0 <= base_range_pct <= 18.0,
        "volatility_contracting": old_volatility is not None and recent_volatility is not None and old_volatility > 0 and recent_volatility <= old_volatility * 0.85,
        "range_contracting": base_range_pct is not None and recent_range_pct is not None and recent_range_pct <= base_range_pct * 0.70,
        "volume_dry_up": volume_dry_up_ratio is not None and volume_dry_up_ratio <= 0.80,
        "support_tested": support_tests >= 2,
        "near_resistance": close_to_resistance_pct is not None and 0.0 <= close_to_resistance_pct <= 3.0,
        "above_base_mean": close_values[-1] >= sma20,
    }
    weights = {"horizontal_base": 18, "volatility_contracting": 18, "range_contracting": 14,
               "volume_dry_up": 18, "support_tested": 12, "near_resistance": 12, "above_base_mean": 8}
    score = sum(weight for key, weight in weights.items() if components[key])
    ready_core = ("horizontal_base", "volatility_contracting", "volume_dry_up", "support_tested", "near_resistance", "above_base_mean")
    status = "ready" if score >= 74 and all(components[key] for key in ready_core) else "forming" if score >= 45 else "not_ready"
    return {
        "status": status, "score": score, "bar_count": len(window), "components": components,
        "metrics": {"base_range_pct": round(base_range_pct, 3) if base_range_pct is not None else None,
                    "recent_range_pct": round(recent_range_pct, 3) if recent_range_pct is not None else None,
                    "old_volatility": round(old_volatility, 6) if old_volatility is not None else None,
                    "recent_volatility": round(recent_volatility, 6) if recent_volatility is not None else None,
                    "volume_dry_up_ratio": round(volume_dry_up_ratio, 3) if volume_dry_up_ratio is not None else None,
                    "support_price": round(support, 4), "support_tests": support_tests,
                    "resistance_price": round(resistance, 4),
                    "close_to_resistance_pct": round(close_to_resistance_pct, 3) if close_to_resistance_pct is not None else None,
                    "sma20": round(sma20, 4)},
        "notice": "盘后蓄势研究结构；未突破阻力且未经过盘中承接确认时，不生成买卖指令。",
    }


def post_close_forming_structure(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Return only a provisional 15-session forming observation."""
    if len(bars) < 15:
        return {"status": "insufficient_history", "bar_count": len(bars), "score": 0}
    window = bars[-15:]
    closes = [_number(row.get("close")) for row in window]
    if any(value is None or value <= 0 for value in closes):
        return {"status": "invalid_prices", "bar_count": len(window), "score": 0}
    values = [float(value) for value in closes if value is not None]
    highs = [float(_number(row.get("high")) or values[index]) for index, row in enumerate(window)]
    lows = [float(_number(row.get("low")) or values[index]) for index, row in enumerate(window)]
    volumes = [_number(row.get("volume")) for row in window]
    base_low, base_high = min(lows), max(highs)
    range_pct = (base_high / base_low - 1) * 100 if base_low > 0 else None
    resistance = max(highs[:-1])
    gap_to_resistance = (resistance / values[-1] - 1) * 100 if values[-1] > 0 else None
    support = min(lows)
    support_tests = sum(1 for low in lows if low <= support * 1.04)
    prior_volumes = [float(value) for value in volumes[-12:-3] if value is not None and value > 0]
    recent_volumes = [float(value) for value in volumes[-3:] if value is not None and value > 0]
    dry_up_ratio = _mean(recent_volumes) / _mean(prior_volumes) if recent_volumes and prior_volumes and _mean(prior_volumes) else None
    first_returns = [values[index] / values[index - 1] - 1 for index in range(1, 8) if values[index - 1] > 0]
    recent_returns = [values[index] / values[index - 1] - 1 for index in range(10, 15) if values[index - 1] > 0]
    old_volatility = math.sqrt(_mean([value * value for value in first_returns])) if first_returns else None
    recent_volatility = math.sqrt(_mean([value * value for value in recent_returns])) if recent_returns else None
    components = {
        "horizontal_range": range_pct is not None and 4.0 <= range_pct <= 18.0,
        "volatility_contracting": old_volatility is not None and recent_volatility is not None and old_volatility > 0 and recent_volatility <= old_volatility * 0.90,
        "volume_dry_up": dry_up_ratio is not None and dry_up_ratio <= 0.80,
        "support_tested": support_tests >= 2,
        "near_resistance": gap_to_resistance is not None and 0 <= gap_to_resistance <= 3.0,
        "above_mean": values[-1] >= _mean(values),
    }
    weights = {"horizontal_range": 24, "volatility_contracting": 20, "volume_dry_up": 20,
               "support_tested": 14, "near_resistance": 14, "above_mean": 8}
    score = sum(weight for key, weight in weights.items() if components[key])
    status = "forming" if score >= 70 and all(components[key] for key in ("horizontal_range", "volume_dry_up", "support_tested", "near_resistance", "above_mean")) else "not_ready"
    return {"status": status, "score": score, "bar_count": len(window), "components": components,
            "metrics": {"base_range_pct": round(range_pct, 3) if range_pct is not None else None,
                        "volume_dry_up_ratio": round(dry_up_ratio, 3) if dry_up_ratio is not None else None,
                        "support_price": round(support, 4), "support_tests": support_tests,
                        "resistance_price": round(resistance, 4),
                        "close_to_resistance_pct": round(gap_to_resistance, 3) if gap_to_resistance is not None else None,
                        "old_volatility": round(old_volatility, 6) if old_volatility is not None else None,
                        "recent_volatility": round(recent_volatility, 6) if recent_volatility is not None else None},
            "notice": "15日形成中结构，只能进入盘后观察池；仍需完整30日基础与次日盘中EAC确认。"}


def post_close_fresh_start_structure(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """Identify an early expansion after a short consolidation, post-close only."""
    if len(bars) < 15:
        return {"status": "insufficient_history", "bar_count": len(bars), "score": 0}
    window = bars[-15:]
    closes = [_number(row.get("close")) for row in window]
    volumes = [_number(row.get("volume")) for row in window]
    if any(value is None or value <= 0 for value in closes):
        return {"status": "invalid_prices", "bar_count": len(window), "score": 0}
    values = [float(value) for value in closes if value is not None]
    current = values[-1]
    return_1d = (current / values[-2] - 1) * 100
    return_3d = (current / values[-4] - 1) * 100
    return_5d = (current / values[-6] - 1) * 100
    prior_volumes = [float(value) for value in volumes[-6:-1] if value is not None and value > 0]
    volume_multiple = float(volumes[-1]) / _mean(prior_volumes) if volumes[-1] is not None and prior_volumes and _mean(prior_volumes) else None
    new_high = current >= max(values[-6:-1])
    components = {
        "controlled_first_day": 3.0 <= return_1d <= 8.0,
        "short_momentum": 2.0 <= return_3d <= 10.0 and 1.0 <= return_5d <= 12.0,
        "volume_expansion": volume_multiple is not None and volume_multiple >= 1.5,
        "new_five_day_high": new_high,
    }
    weights = {"controlled_first_day": 30, "short_momentum": 28, "volume_expansion": 26, "new_five_day_high": 16}
    score = sum(weight for key, weight in weights.items() if components[key])
    return {"status": "started" if score >= 84 and all(components.values()) else "not_ready", "score": score,
            "bar_count": len(window), "components": components,
            "metrics": {"return_1d_pct": round(return_1d, 3), "return_3d_pct": round(return_3d, 3),
                        "return_5d_pct": round(return_5d, 3), "volume_multiple_5d": round(volume_multiple, 3) if volume_multiple is not None else None},
            "notice": "收盘后首动观察，不追认同日盘中买点；次日仅在EAC扩张、承接与风险门禁齐备时继续观察。"}


__all__ = [
    "POST_CLOSE_STRATEGY_MODEL_VERSION",
    "daily_base_structure",
    "post_close_forming_structure",
    "post_close_fresh_start_structure",
]
