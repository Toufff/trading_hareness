"""Parse THS order-query exports without pretending order time is fill time."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo

from .broker_export_parser import normalize_symbol


CN = ZoneInfo("Asia/Shanghai")


class BrokerOrderExportError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedOrderExport:
    events: list[dict[str, Any]]
    executions: list[dict[str, Any]]
    ignored_rows: list[dict[str, Any]]
    encoding: str
    sha256: str
    header_row: int
    account_fingerprint: str
    masked_account: str
    min_order_date: date
    max_order_date: date


def _header(value: object) -> str:
    return re.sub(r"[\s（）()：:._\-/]+", "", str(value or "").strip()).casefold()


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object, label: str) -> Decimal:
    raw = _text(value).replace(",", "").replace("￥", "").replace("元", "")
    if raw in {"", "--", "-", "—"}:
        return Decimal(0)
    try:
        result = Decimal(raw)
    except InvalidOperation as error:
        raise BrokerOrderExportError(f"BROKER_ORDER_NUMBER_INVALID: {label}") from error
    if not result.is_finite() or result < 0:
        raise BrokerOrderExportError(f"BROKER_ORDER_NUMBER_INVALID: {label}")
    return result


def _read(path: Path) -> tuple[list[list[str]], str, str]:
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or not 0 < path.stat().st_size <= 50_000_000:
        raise BrokerOrderExportError("BROKER_ORDER_EXPORT_FILE_INVALID")
    body = path.read_bytes()
    if body.startswith(b"\xd0\xcf\x11\xe0") or body.startswith(b"PK\x03\x04"):
        raise BrokerOrderExportError("BROKER_ORDER_BINARY_SPREADSHEET_UNSUPPORTED")
    decoded = None
    encoding = ""
    for candidate in ("utf-8-sig", "gb18030"):
        try:
            decoded = body.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if decoded is None:
        raise BrokerOrderExportError("BROKER_ORDER_EXPORT_ENCODING_UNSUPPORTED")
    delimiter = "\t" if "\t" in decoded else ","
    rows = [[cell.strip() for cell in row] for row in csv.reader(decoded.splitlines(), delimiter=delimiter)]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise BrokerOrderExportError("BROKER_ORDER_EXPORT_EMPTY")
    return rows, encoding, sha256(body).hexdigest()


ALIASES = {
    "order_date": ("委托日期",), "order_time": ("委托时间",),
    "symbol": ("证券代码", "股票代码", "代码"), "name": ("证券名称", "股票名称", "名称"),
    "side": ("买卖标志",), "status": ("状态说明",), "order_quantity": ("委托数量",),
    "filled_quantity": ("成交数量",), "gross_amount": ("成交金额",), "business_type": ("业务类型",),
    "order_price": ("委托价格",), "cancel_requested_quantity": ("撤消数量", "撤销数量"),
    "fill_price": ("成交价格",), "order_number": ("委托编号",), "market": ("交易市场",),
    "cancelled_quantity": ("已撤数量",), "order_kind": ("委托类别",), "cancel_flag": ("撤销标志",),
    "fund_account": ("资金账户", "资金账号"),
}


def _columns(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    required = {"order_date", "order_time", "symbol", "name", "side", "status", "order_quantity",
                "filled_quantity", "gross_amount", "order_price", "fill_price", "order_number", "fund_account"}
    normalized = {key: {_header(alias) for alias in aliases} for key, aliases in ALIASES.items()}
    for row_index, row in enumerate(rows[:30]):
        positions = {_header(value): index for index, value in enumerate(row) if _header(value)}
        mapped = {key: next((positions[alias] for alias in aliases if alias in positions), None)
                  for key, aliases in normalized.items()}
        if all(mapped[key] is not None for key in required):
            return row_index, {key: value for key, value in mapped.items() if value is not None}
    raise BrokerOrderExportError("BROKER_ORDER_EXPORT_HEADER_MISMATCH")


def _value(row: list[str], columns: dict[str, int], key: str) -> str:
    index = columns.get(key)
    return row[index] if index is not None and index < len(row) else ""


def _order_date(value: object) -> date:
    raw = re.sub(r"\D", "", _text(value))
    if len(raw) != 8:
        raise BrokerOrderExportError("BROKER_ORDER_DATE_INVALID")
    return datetime.strptime(raw, "%Y%m%d").date()


def _order_at(day: date, value: object) -> datetime:
    raw = re.sub(r"\D", "", _text(value)).zfill(6)
    if len(raw) != 6:
        raise BrokerOrderExportError("BROKER_ORDER_TIME_INVALID")
    parsed = datetime.strptime(raw, "%H%M%S").time()
    return datetime.combine(day, parsed, tzinfo=CN)


def _mask_account(value: str) -> str:
    return f"{value[:2]}****{value[-4:]}" if len(value) >= 7 else "****"


def _side(value: str) -> str | None:
    return "buy" if "买" in value else "sell" if "卖" in value else None


def parse_order_export(path: Path) -> ParsedOrderExport:
    rows, encoding, source_hash = _read(Path(path))
    header_row, columns = _columns(rows)
    accounts = {_text(_value(row, columns, "fund_account")) for row in rows[header_row + 1:] if any(row)}
    accounts.discard("")
    if len(accounts) != 1:
        code = "BROKER_ORDER_EXPORT_MULTIPLE_ACCOUNTS" if len(accounts) > 1 else "BROKER_ORDER_EXPORT_ACCOUNT_MISSING"
        raise BrokerOrderExportError(code)
    account = next(iter(accounts))
    account_fingerprint = sha256(("ths-fund-account:" + account).encode("utf-8")).hexdigest()
    events: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[header_row + 1:], start=header_row + 2):
        try:
            day = _order_date(_value(row, columns, "order_date"))
            order_at = _order_at(day, _value(row, columns, "order_time"))
        except BrokerOrderExportError:
            ignored.append({"line": line_number, "reason": "invalid_date_or_time"})
            continue
        raw_symbol = _text(_value(row, columns, "symbol"))
        try:
            symbol = normalize_symbol(raw_symbol)
        except ValueError:
            symbol = None
        raw_side = _text(_value(row, columns, "side"))
        normalized_side = _side(raw_side)
        # Non-trading bookkeeping rows (for example 799999 指定登记) are not
        # instruments and must never leak into the security master.
        if normalized_side is None:
            symbol = None
        status = _text(_value(row, columns, "status"))
        values = {
            "order_quantity": _decimal(_value(row, columns, "order_quantity"), "order_quantity"),
            "filled_quantity": _decimal(_value(row, columns, "filled_quantity"), "filled_quantity"),
            "gross_amount": _decimal(_value(row, columns, "gross_amount"), "gross_amount"),
            "order_price": _decimal(_value(row, columns, "order_price"), "order_price"),
            "fill_price": _decimal(_value(row, columns, "fill_price"), "fill_price"),
            "cancel_requested_quantity": _decimal(_value(row, columns, "cancel_requested_quantity"), "cancel_requested_quantity"),
            "cancelled_quantity": _decimal(_value(row, columns, "cancelled_quantity"), "cancelled_quantity"),
        }
        canonical = {
            "account_fingerprint": account_fingerprint, "order_date": day.isoformat(),
            "order_time": order_at.timetz().replace(tzinfo=None).isoformat(), "symbol": symbol or raw_symbol,
            "name": _text(_value(row, columns, "name")), "side": raw_side, "status": status,
            "order_number": _text(_value(row, columns, "order_number")),
            "business_type": _text(_value(row, columns, "business_type")),
            "market": _text(_value(row, columns, "market")), "order_kind": _text(_value(row, columns, "order_kind")),
            "cancel_flag": _text(_value(row, columns, "cancel_flag")),
            **{key: str(value) for key, value in values.items()},
        }
        event_key = sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if event_key in seen:
            ignored.append({"line": line_number, "reason": "duplicate_event_in_export", "event_key": event_key})
            continue
        seen.add(event_key)
        event = {
            "event_key": event_key, "order_date": day, "order_at": order_at, "symbol": symbol,
            "raw_symbol": raw_symbol, "name": canonical["name"], "side": normalized_side, "raw_side": raw_side,
            "status": status, "business_type": canonical["business_type"], "order_number": canonical["order_number"],
            "market": canonical["market"], "order_kind": canonical["order_kind"], "cancel_flag": canonical["cancel_flag"],
            **values, "metadata": {"source_line": line_number},
        }
        events.append(event)
        strict_execution = (
            event["side"] in {"buy", "sell"} and event["symbol"] is not None
            and status in {"全部成交", "部成部撤"}
            and values["filled_quantity"] > 0 and values["fill_price"] > 0 and values["gross_amount"] > 0
        )
        if strict_execution:
            executions.append({
                "execution_key": sha256(("ths-order-execution:" + event_key).encode()).hexdigest(),
                "source_event_key": event_key, "order_date": day, "order_at": order_at,
                "symbol": symbol, "name": event["name"], "side": event["side"], "status": status,
                "quantity": values["filled_quantity"], "price": values["fill_price"],
                "gross_amount": values["gross_amount"], "time_basis": "order_time_proxy",
                "metadata": {"order_number": event["order_number"], "source_line": line_number},
            })
    if not events:
        raise BrokerOrderExportError("BROKER_ORDER_EXPORT_NO_EVENTS")
    dates = [row["order_date"] for row in events]
    return ParsedOrderExport(
        events=events, executions=executions, ignored_rows=ignored, encoding=encoding, sha256=source_hash,
        header_row=header_row + 1, account_fingerprint=account_fingerprint, masked_account=_mask_account(account),
        min_order_date=min(dates), max_order_date=max(dates),
    )


__all__ = ["BrokerOrderExportError", "ParsedOrderExport", "parse_order_export"]
