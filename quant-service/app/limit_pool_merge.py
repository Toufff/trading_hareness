"""Deterministic, source-preserving merge for persisted limit-up pools."""

from __future__ import annotations

import json
import re
from typing import Any, Callable


_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)")


def merge_limit_pool_sources(
    ths_rows: list[dict[str, Any]],
    eastmoney_rows: list[dict[str, Any]],
    *,
    json_safe: Callable[[Any], Any],
    number: Callable[[Any], float | None],
) -> dict[str, Any]:
    """Build a source-labelled union without claiming exchange completeness.

    A source can enrich a matching security only when the primary source did
    not provide that field.  Chinese labels are never used for joins: the
    canonical six-digit exchange code is the sole identity.
    """
    merged: dict[str, dict[str, Any]] = {}
    ths_symbols: set[str] = set()
    eastmoney_symbols: set[str] = set()
    for stored in ths_rows:
        raw = dict(stored.get("row_data") or stored)
        symbol = str(raw.get("ts_code") or "").upper()
        if not _SYMBOL.fullmatch(symbol):
            continue
        ths_symbols.add(symbol)
        merged[symbol] = {
            **json_safe(raw), "ts_code": symbol,
            "provider_key": stored.get("provider_key") or "tushare_super_sdk",
            "available_at": stored.get("available_at"), "sources": ["tushare_limit_list_ths"],
        }
    for stored in eastmoney_rows:
        symbol = str(stored.get("symbol") or "").upper()
        if not _SYMBOL.fullmatch(symbol):
            continue
        eastmoney_symbols.add(symbol)
        body = stored.get("body") or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = {}
        raw = dict(body) if isinstance(body, dict) else {}
        board_count = int(number(raw.get("连板数")) or 1)
        eastmoney = {
            "ts_code": symbol, "name": raw.get("名称"), "limit_type": "涨停池",
            "pct_chg": number(raw.get("涨跌幅")), "price": number(raw.get("最新价")),
            "amount": number(raw.get("成交额")), "turnover_rate": number(raw.get("换手率")),
            "limit_amount": number(raw.get("封板资金")), "first_time": raw.get("首次封板时间"),
            "last_time": raw.get("最后封板时间"), "open_num": number(raw.get("炸板次数")),
            "tag": "首板" if board_count <= 1 else f"{board_count}连板",
            "lu_desc": raw.get("所属行业"), "provider_key": stored.get("source") or "akshare",
            "available_at": stored.get("available_at"), "sources": ["eastmoney_stock_zt_pool_em"],
        }
        if symbol in merged:
            for key, value in eastmoney.items():
                if key not in {"provider_key", "available_at", "sources"} and merged[symbol].get(key) in {None, ""} and value not in {None, ""}:
                    merged[symbol][key] = value
            merged[symbol]["sources"] = [*merged[symbol].get("sources", []), "eastmoney_stock_zt_pool_em"]
            merged[symbol]["eastmoney_evidence"] = json_safe(eastmoney)
        else:
            merged[symbol] = eastmoney
    union_symbols = ths_symbols | eastmoney_symbols
    return {
        "items": list(merged.values()),
        "coverage": {
            "status": "two_source_union" if ths_symbols and eastmoney_symbols else "single_source_only",
            "union_count": len(union_symbols), "intersection_count": len(ths_symbols & eastmoney_symbols),
            "tushare_count": len(ths_symbols), "eastmoney_count": len(eastmoney_symbols),
            "tushare_only": sorted(ths_symbols - eastmoney_symbols),
            "eastmoney_only": sorted(eastmoney_symbols - ths_symbols), "local_truncation": False,
            "notice": "完整表示当前已访问同花顺与东财涨停池的去重并集，不代表交易所官方全量保证。",
        },
    }


__all__ = ["merge_limit_pool_sources"]
