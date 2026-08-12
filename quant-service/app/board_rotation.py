"""Pure one-minute board-flow rotation detection and message rendering.

Eastmoney board flow is an indicative public-source aggregate, not exchange
order flow.  These helpers deliberately compare only adjacent snapshots from
the same source/taxonomy and produce human-review alerts, never orders.
"""

from __future__ import annotations

import math
import os
from typing import Any


def board_rotation_min_delta() -> float:
    try:
        return min(100.0, max(0.1, float(os.getenv("INTRADAY_BOARD_ROTATION_MIN_DELTA", "2.0"))))
    except ValueError:
        return 2.0


def board_rotation_min_abs_net() -> float:
    try:
        return min(100.0, max(0.1, float(os.getenv("INTRADAY_BOARD_ROTATION_MIN_ABS_NET", "1.0"))))
    except ValueError:
        return 1.0


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _key(item: dict[str, Any]) -> tuple[str, str] | None:
    taxonomy = str(item.get("taxonomy_key") or "").strip()
    sector = str(item.get("sector_key") or "").strip()
    return (taxonomy, sector) if taxonomy and sector else None


def board_rotation_candidates(previous_items: list[dict[str, Any]], current_items: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    """Find the largest same-taxonomy one-minute net-flow changes.

    A threshold combines a configured absolute floor with the 95th percentile
    of absolute changes for that taxonomy.  It remains meaningful when the
    upstream units are indicative, while the explicit floor prevents a quiet
    market's tiny numerical changes becoming alerts.
    """
    previous = {_key(item): item for item in previous_items if _key(item) is not None and _number(item.get("net_inflow")) is not None}
    current = {_key(item): item for item in current_items if _key(item) is not None and _number(item.get("net_inflow")) is not None}
    changes: dict[str, list[float]] = {}
    paired: list[tuple[dict[str, Any], float, float, float]] = []
    for key, item in current.items():
        prior = previous.get(key)
        if prior is None:
            continue
        before, after = _number(prior.get("net_inflow")), _number(item.get("net_inflow"))
        if before is None or after is None:
            continue
        delta = after - before
        paired.append((item, before, after, delta))
        changes.setdefault(str(item["taxonomy_key"]), []).append(abs(delta))
    thresholds: dict[str, float] = {}
    for taxonomy, values in changes.items():
        ordered = sorted(values)
        percentile = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]
        thresholds[taxonomy] = max(board_rotation_min_delta(), percentile)
    candidates: list[dict[str, Any]] = []
    for item, before, after, delta in paired:
        taxonomy = str(item["taxonomy_key"])
        threshold = thresholds[taxonomy]
        if abs(delta) < threshold or abs(after) < board_rotation_min_abs_net():
            continue
        direction = "inflow" if after > 0 else "outflow"
        crossed_zero = (before < 0 < after) or (before > 0 > after)
        event_type = "cross_zero" if crossed_zero else "flow_surge"
        candidates.append({
            "event_key": f"board_rotation:{taxonomy}:{item['sector_key']}:{event_type}:{direction}",
            "taxonomy_key": taxonomy, "sector_key": str(item["sector_key"]), "label": str(item.get("label") or item["sector_key"]),
            "event_type": event_type, "direction": direction,
            "previous_net_inflow": round(before, 6), "current_net_inflow": round(after, 6),
            "delta_net_inflow": round(delta, 6), "dynamic_threshold": round(threshold, 6),
            "change_pct": _number(item.get("change_pct")),
        })
    candidates.sort(key=lambda item: (-abs(float(item["delta_net_inflow"])), str(item["event_key"])))
    return candidates[:max(1, min(10, limit))]


def board_rotation_still_directional(event: dict[str, Any], current_items: list[dict[str, Any]]) -> bool:
    """Require the next minute to retain the detected flow direction."""
    for item in current_items:
        if str(item.get("taxonomy_key")) != str(event.get("taxonomy_key")) or str(item.get("sector_key")) != str(event.get("sector_key")):
            continue
        value = _number(item.get("net_inflow"))
        if value is None or abs(value) < board_rotation_min_abs_net():
            return False
        return value > 0 if event.get("direction") == "inflow" else value < 0
    return False


def _flow_label(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:+.2f} 亿"


def board_rotation_alert_text(event: dict[str, Any]) -> str:
    """Render a compact, evidence-first board rotation review prompt."""
    event_type = "流出转流入" if event.get("event_type") == "cross_zero" and event.get("direction") == "inflow" else \
                 "流入转流出" if event.get("event_type") == "cross_zero" else \
                 "流入加速" if event.get("direction") == "inflow" else "流出加速"
    taxonomy = "概念" if str(event.get("taxonomy_key", "")).endswith("concept") else "行业"
    lines = [
        f"【板块资金轮动确认｜{event_type}】",
        f"{taxonomy}｜{event.get('label')}（{event.get('sector_key')}）",
        f"观测时间（上海）：{event.get('observed_at_shanghai', '—')}",
        f"前一分钟净流 { _flow_label(event.get('previous_net_inflow')) } → 当前 { _flow_label(event.get('current_net_inflow')) }，变化 { _flow_label(event.get('delta_net_inflow')) }。",
        f"同类板块动态门槛 { _flow_label(event.get('dynamic_threshold')) }｜板块涨跌 {event.get('change_pct', '—')}%。",
        "来源：东财一分钟板块资金快照；已由下一分钟方向确认。仅作板块联动人工复核，不构成交易指令。",
    ]
    return "\n".join(lines)


__all__ = [
    "board_rotation_alert_text", "board_rotation_candidates", "board_rotation_min_abs_net",
    "board_rotation_min_delta", "board_rotation_still_directional",
]
