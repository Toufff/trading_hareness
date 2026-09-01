"""Pure merge and control derivation for the Longhu/Tencent close source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping


PROVIDER_KEY = "longhuvip_composite"
FLOW_SOURCE = "longhuvip_main_net"


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@dataclass(frozen=True)
class MergedCrossSection:
    daily_rows: list[dict[str, Any]]
    fundamental_rows: list[dict[str, Any]]
    flow_rows: list[dict[str, Any]]
    quote_rows: list[dict[str, Any]]
    coverage: float
    close_conflicts: tuple[dict[str, Any], ...]


def merge_cross_section(
    trade_date: date,
    vendor_rows: Mapping[str, Mapping[str, Any]],
    quote_rows: list[dict[str, Any]],
) -> MergedCrossSection:
    """Require same-session OHLC and retain the exact vendor flow convention."""
    expected_date = trade_date.strftime("%Y%m%d")
    quotes = {
        str(row.get("ts_code") or "").upper(): row
        for row in quote_rows
        if str(row.get("trade_date") or "").replace("-", "") == expected_date
    }
    daily: list[dict[str, Any]] = []
    fundamentals: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for symbol, vendor in sorted(vendor_rows.items()):
        quote = quotes.get(symbol)
        if not quote:
            continue
        vendor_close, quote_close = _decimal(vendor.get("close")), _decimal(quote.get("close"))
        if vendor_close is None or quote_close is None or quote_close <= 0:
            continue
        difference = abs(vendor_close - quote_close) / quote_close
        if difference > Decimal("0.005"):
            conflicts.append({
                "symbol": symbol, "vendor_close": str(vendor_close),
                "tencent_close": str(quote_close), "relative_difference": str(difference),
            })
            continue
        name = str(vendor.get("name") or quote.get("name") or symbol)
        daily.append({
            "ts_code": symbol, "trade_date": expected_date, "name": name,
            "open": quote.get("open"), "high": quote.get("high"), "low": quote.get("low"),
            "close": quote.get("close"), "pre_close": quote.get("pre_close"),
            "vol": quote.get("vol"), "amount": quote.get("amount"),
        })
        fundamentals.append({
            "ts_code": symbol, "trade_date": expected_date, "close": quote.get("close"),
            "turnover_rate": vendor.get("turnover_rate"), "volume_ratio": vendor.get("volume_ratio"),
            "pe": vendor.get("pe"), "pb": vendor.get("pb"),
            # Longhu reports market value in CNY while Tushare's canonical
            # daily_basic contract is 10k CNY.  Convert at this boundary.
            "total_mv": (float(_decimal(vendor.get("total_mv")) / Decimal("10000"))
                         if _decimal(vendor.get("total_mv")) is not None else None),
            "circ_mv": (float(_decimal(vendor.get("circ_mv")) / Decimal("10000"))
                        if _decimal(vendor.get("circ_mv")) is not None else None),
            "source_semantics": "longhuvip_industry_cross_section",
        })
        flows.append({
            "symbol": symbol, "trading_date": trade_date, "source": FLOW_SOURCE,
            "net_amount": vendor.get("main_net"), "net_amount_rate": None,
            "buy_elg_amount": None, "buy_lg_amount": None,
            "buy_md_amount": None, "buy_sm_amount": None,
            "raw": {
                **dict(vendor.get("raw") or {}),
                "flow_convention": vendor.get("flow_convention"),
                "semantic_warning": "order_size_classified_not_institution_identity_not_level2",
            },
        })
        snapshots.append({
            **quote, "ts_code": symbol, "name": name,
            "pct_chg": vendor.get("pct_chg") if vendor.get("pct_chg") is not None else quote.get("pct_chg"),
            "turnover_rate": vendor.get("turnover_rate"), "volume_ratio": vendor.get("volume_ratio"),
            "main_net": vendor.get("main_net"), "provider_basis": "longhuvip+tencent",
            "flow_convention": vendor.get("flow_convention"),
        })
    coverage = len(daily) / len(vendor_rows) if vendor_rows else 0.0
    return MergedCrossSection(daily, fundamentals, flows, snapshots, coverage, tuple(conflicts))


def _limit_ratio(symbol: str, name: str) -> tuple[Decimal, str]:
    normalized_name = name.upper().replace("*", "")
    if "ST" in normalized_name:
        return Decimal("0.05"), "st_5_percent"
    code, exchange = symbol.split(".")
    if exchange == "BJ":
        return Decimal("0.30"), "beijing_30_percent"
    if code.startswith(("300", "301", "688", "689")):
        return Decimal("0.20"), "registration_board_20_percent"
    return Decimal("0.10"), "mainboard_10_percent"


def build_control_rows(daily_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build transparent same-day controls without claiming corporate-action history."""
    limits: list[dict[str, Any]] = []
    factors: list[dict[str, Any]] = []
    for row in daily_rows:
        symbol, name = str(row["ts_code"]), str(row.get("name") or "")
        pre_close = _decimal(row.get("pre_close"))
        if pre_close is None or pre_close <= 0:
            continue
        ratio, rule = _limit_ratio(symbol, name)
        quantum = Decimal("0.01")
        limits.append({
            "ts_code": symbol, "trade_date": row["trade_date"],
            "up_limit": str((pre_close * (Decimal("1") + ratio)).quantize(quantum, rounding=ROUND_HALF_UP)),
            "down_limit": str((pre_close * (Decimal("1") - ratio)).quantize(quantum, rounding=ROUND_HALF_UP)),
            "derivation": "preclose_times_board_limit_ratio", "board_rule": rule,
            "exception_warning": "IPO/resumption/no-limit exceptions are not inferred",
        })
        factors.append({
            "ts_code": symbol, "trade_date": row["trade_date"], "adj_factor": "1",
            "factor_semantics": "same_day_identity_only",
            "warning": "not a historical corporate-action adjustment factor",
        })
    return {"stk_limit": limits, "adj_factor": factors}


__all__ = [
    "FLOW_SOURCE", "MergedCrossSection", "PROVIDER_KEY", "build_control_rows", "merge_cross_section",
]
