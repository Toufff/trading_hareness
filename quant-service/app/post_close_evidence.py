"""Pure post-close board and LHB evidence aggregation."""

from __future__ import annotations

from typing import Any, Callable


def exact_board_context(rows: list[dict[str, Any]], *, json_safe: Callable[[Any], Any]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = json_safe(dict(row))
        symbol = str(item["symbol"])
        current = contexts.get(symbol)
        if current is None or float(item.get("net_amount") or 0) > float(current.get("net_amount") or 0):
            contexts[symbol] = {**item, "exact_member_mapping": True}
    positive_flows = sorted({float(item.get("net_amount") or 0) for item in contexts.values()
                             if float(item.get("net_amount") or 0) > 0})
    denominator = max(1, len(positive_flows) - 1)
    for item in contexts.values():
        flow = float(item.get("net_amount") or 0)
        item["flow_percentile"] = round(positive_flows.index(flow) / denominator, 4) if flow > 0 else 0.0
    return contexts


def lhb_context(rows: list[dict[str, Any]], *, number: Callable[[Any], float | None]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    seen_inst: set[tuple[str, str, float, float, float]] = set()
    seen_list: set[tuple[str, str]] = set()
    for stored in rows:
        raw = dict(stored.get("row_data") or {})
        symbol = str(raw.get("ts_code") or "").upper()
        if not symbol:
            continue
        context = contexts.setdefault(symbol, {
            "trade_date": raw.get("trade_date"), "top_list_rows": 0, "institution_records": 0,
            "institution_buy": 0.0, "institution_sell": 0.0, "institution_net_buy": 0.0,
            "institutions": [], "reasons": [], "providers": [], "available_at": stored.get("available_at"),
        })
        context["providers"] = list(dict.fromkeys([*context["providers"], str(stored.get("provider_key") or "")]))
        reason = str(raw.get("reason") or "").strip()
        if reason:
            context["reasons"] = list(dict.fromkeys([*context["reasons"], reason]))
        if stored.get("api_name") == "top_inst":
            institution = str(raw.get("exalter") or "机构席位").strip()
            buy = float(number(raw.get("buy")) or 0)
            sell = float(number(raw.get("sell")) or 0)
            net_buy = float(number(raw.get("net_buy")) or (buy - sell))
            key = (symbol, institution, buy, sell, net_buy)
            if key in seen_inst:
                continue
            seen_inst.add(key)
            context["institution_records"] += 1
            context["institution_buy"] += buy
            context["institution_sell"] += sell
            context["institution_net_buy"] += net_buy
            context["institutions"] = list(dict.fromkeys([*context["institutions"], institution]))
        else:
            key = (symbol, reason)
            if key in seen_list:
                continue
            seen_list.add(key)
            context["top_list_rows"] += 1
    for context in contexts.values():
        for key in ("institution_buy", "institution_sell", "institution_net_buy"):
            context[key] = round(float(context[key]), 2)
        context["institution_count"] = len(context["institutions"])
    return contexts


__all__ = ["exact_board_context", "lhb_context"]
