"""Human-readable, holdings-independent research for user-tracked stocks.

The durable tracking tag answers *why the symbol is present*.  This module
answers *what is currently known about it*.  It projects the existing stock
workbench into one compact, auditable snapshot without granting a buy action.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any


TRACKING_RESEARCH_VERSION = "user-tracking-research-v1"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def has_active_user_tracking_tag(metadata: Any) -> bool:
    values = _items(_mapping(metadata).get("tracking_tags"))
    return any(
        str(item.get("key") or "") == "user_requested_tracking"
        and str(item.get("source") or "user") == "user"
        and item.get("active") is not False
        for item in values
    )


def _scenario_condition(view: dict[str, Any], state: str) -> str | None:
    for item in _items(view.get("next_session")):
        if item.get("state") == state:
            value = str(item.get("condition") or "").strip()
            return value or None
    return None


def _stance(
    *, close: float | None, ma20: float | None, flow5: float | None,
    sector_net: float | None, amount_multiple: float | None,
) -> tuple[str, str]:
    weak_price = close is not None and ma20 is not None and close < ma20
    weak_flow = flow5 is not None and flow5 < 0
    weak_sector = sector_net is not None and sector_net < 0
    if weak_price and weak_flow and weak_sector:
        return "risk_repair", "价格位于20日均线下方，近5日同口径资金与所属板块资金均为负，当前首先观察风险修复。"
    strong_price = close is not None and ma20 is not None and close >= ma20
    strong_flow = flow5 is not None and flow5 > 0
    strong_sector = sector_net is not None and sector_net > 0
    active_volume = amount_multiple is not None and amount_multiple >= 1.0
    if strong_price and strong_flow and strong_sector and active_volume:
        return "strengthening", "价格、近5日同口径资金、板块资金与成交活跃度形成同向确认，进入强化跟踪。"
    return "mixed_watch", "价格、资金、板块或量能尚未形成一致方向，保留跟踪并等待条件收敛。"


def build_snapshot(workbench: dict[str, Any]) -> dict[str, Any]:
    """Project a full workbench into a compact manual-tracking analysis."""
    series = _mapping(workbench.get("series"))
    daily = _items(series.get("daily"))
    latest = daily[-1] if daily else {}
    technical = _mapping(workbench.get("technical_summary"))
    flow = _mapping(workbench.get("flow"))
    windows = _mapping(flow.get("windows"))
    sectors = _items(workbench.get("sectors"))
    sector = sectors[0] if sectors else {}
    view = next(
        (item for item in _items(workbench.get("strategy_views")) if item.get("key") == "user_tracking"),
        {},
    )
    close = _number(technical.get("close") if technical.get("close") is not None else latest.get("close"))
    ma20 = _number(technical.get("ma20"))
    flow5 = _number(_mapping(windows.get("5")).get("net_amount"))
    sector_net = _number(sector.get("net_amount") if sector.get("net_amount") is not None else sector.get("net_inflow"))
    amount_multiple = _number(technical.get("amount_multiple_5"))
    stance, headline = _stance(
        close=close, ma20=ma20, flow5=flow5, sector_net=sector_net,
        amount_multiple=amount_multiple,
    )
    health = _mapping(workbench.get("data_health"))
    required = ("price", "volume", "vendor_flow", "sector", "messages", "scenario")
    unavailable = [key for key in required if _mapping(health.get(key)).get("status") == "unavailable"]
    events = [{
        "title": item.get("title"), "category": item.get("category"),
        "verification": item.get("verification"), "occurred_at": item.get("occurred_at"),
        "available_at": item.get("available_at"), "url": item.get("url"),
    } for item in _items(workbench.get("messages"))[:8]]
    normalized_windows = {
        key: {
            "net_amount": _number(_mapping(windows.get(key)).get("net_amount")),
            "positive_days": _mapping(windows.get(key)).get("positive_days"),
            "observations": _mapping(windows.get(key)).get("observations"),
        }
        for key in ("1", "3", "5", "10")
        if _mapping(windows.get(key))
    }
    return _json_safe({
        "version": TRACKING_RESEARCH_VERSION,
        "status": "degraded" if unavailable else "complete",
        "stance": stance,
        "headline": headline,
        "symbol": workbench.get("symbol"), "name": workbench.get("name"),
        "industry": workbench.get("industry"), "as_of_date": workbench.get("as_of_date"),
        "generated_at": workbench.get("generated_at"),
        "price_structure": {
            "close": close, "change_pct": _number(latest.get("change_pct")),
            "ma5": _number(technical.get("ma5")), "ma20": ma20,
            "support": _number(technical.get("support_5")),
            "resistance": _number(technical.get("resistance_10")),
            "atr14": _number(technical.get("atr14")), "rsi14": _number(technical.get("rsi14")),
            "return_5d_pct": _number(technical.get("return_5d_pct")),
            "return_20d_pct": _number(technical.get("return_20d_pct")),
        },
        "liquidity": {
            "amount": _number(latest.get("amount")), "turnover_rate": _number(latest.get("turnover_rate")),
            "amount_multiple_5": amount_multiple, "volume_ratio": _number(latest.get("volume_ratio")),
        },
        "valuation": {"pe": _number(latest.get("pe")), "pb": _number(latest.get("pb"))},
        "capital_flow": {
            "status": flow.get("status"), "windows": normalized_windows,
            "semantic_boundary": flow.get("semantic_boundary"),
        },
        "sector": {
            "label": sector.get("label") or workbench.get("industry"),
            "change_pct": _number(sector.get("change_pct")), "net_amount": sector_net,
            "status": sector.get("status"),
        },
        "market": _mapping(workbench.get("market_context")),
        "events": events,
        "conditions": {
            "confirmation": _scenario_condition(view, "向上确认"),
            "range": _scenario_condition(view, "区间消化"),
            "invalidation": _scenario_condition(view, "向下失效"),
        },
        "current_reading": [str(value) for value in view.get("current_reading", []) if value],
        "risks": [
            headline,
            "消息仅使用带来源和可得时间的记录；未取得消息不能解释为没有风险。",
            "成交单规模资金不等同真实机构身份、暗盘或Level-2撤单。",
        ],
        "data_health": {key: _mapping(health.get(key)) for key in required},
        "artifact_freshness": _mapping(workbench.get("artifact_freshness")),
        "unavailable_sections": unavailable,
        "buy_authorized": False,
        "depends_on_holdings": False,
        "notice": "用户主动跟踪保证持续复评，不等同买入授权；策略命中会作为独立标签追加。",
    })


__all__ = ["TRACKING_RESEARCH_VERSION", "build_snapshot", "has_active_user_tracking_tag"]
