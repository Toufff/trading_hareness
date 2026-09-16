"""Parse read-only exports produced by THS broker clients.

The free Windows client commonly writes a tab-separated GB18030 text file
with an ``.xls`` suffix.  This module deliberately accepts only text tables;
it never opens a broker client and never treats a hand-written JSON document
as broker evidence.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Iterable


class BrokerExportError(ValueError):
    pass


def _header(value: object) -> str:
    return re.sub(r"[\s（）()：:._\-/]+", "", str(value or "").strip()).casefold()


def _text(value: object) -> str:
    return str(value or "").strip()


def _decimal(value: object, label: str, *, required: bool = True, signed: bool = False) -> Decimal | None:
    raw = _text(value).replace(",", "").replace("￥", "").replace("元", "")
    if raw in {"", "--", "-", "—"}:
        if required:
            raise BrokerExportError(f"BROKER_EXPORT_FIELD_MISSING: {label}")
        return None
    if raw.endswith("%"):
        raw = raw[:-1]
    try:
        result = Decimal(raw)
    except InvalidOperation as error:
        raise BrokerExportError(f"BROKER_EXPORT_NUMBER_INVALID: {label}") from error
    if not result.is_finite() or (not signed and result < 0):
        raise BrokerExportError(f"BROKER_EXPORT_NUMBER_INVALID: {label}")
    return result


def _decode_table(path: Path) -> tuple[list[list[str]], str, str]:
    path = Path(path)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > 50_000_000:
        raise BrokerExportError("BROKER_EXPORT_FILE_INVALID")
    body = path.read_bytes()
    if body.startswith(b"\xd0\xcf\x11\xe0") or body.startswith(b"PK\x03\x04"):
        raise BrokerExportError("BROKER_EXPORT_BINARY_SPREADSHEET_UNSUPPORTED")
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
        raise BrokerExportError("BROKER_EXPORT_ENCODING_UNSUPPORTED")
    delimiter = "\t" if "\t" in decoded else ","
    rows = [[cell.strip() for cell in row] for row in csv.reader(decoded.splitlines(), delimiter=delimiter)]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise BrokerExportError("BROKER_EXPORT_EMPTY")
    return rows, encoding, sha256(body).hexdigest()


def _find_header(rows: list[list[str]], required_groups: Iterable[set[str]]) -> tuple[int, dict[str, int]]:
    required_groups = list(required_groups)
    for index, row in enumerate(rows[:30]):
        columns = {_header(value): offset for offset, value in enumerate(row) if _header(value)}
        if all(any(alias in columns for alias in group) for group in required_groups):
            return index, columns
    raise BrokerExportError("BROKER_EXPORT_HEADER_MISMATCH")


def _cell(row: list[str], columns: dict[str, int], aliases: set[str], *, required: bool = True) -> str | None:
    for alias in aliases:
        offset = columns.get(alias)
        if offset is not None:
            value = row[offset] if offset < len(row) else ""
            if value or not required:
                return value
    if required:
        raise BrokerExportError("BROKER_EXPORT_COLUMN_MISSING: " + "/".join(sorted(aliases)))
    return None


SYMBOL = {_header(value) for value in ("证券代码", "股票代码", "代码")}
NAME = {_header(value) for value in ("证券名称", "证券简称", "股票名称", "名称")}
QUANTITY = {_header(value) for value in ("实际数量", "股份余额", "股票余额", "证券数量", "持仓数量", "当前持仓")}
SELLABLE = {_header(value) for value in ("可用股份", "可卖数量", "可用余额", "可用数量", "可卖")}
COST = {_header(value) for value in ("成本价", "成本价格", "摊薄成本价", "参考成本价")}
PRICE = {_header(value) for value in ("市价", "最新价", "当前价", "参考市价")}
MARKET_VALUE = {_header(value) for value in ("市值", "证券市值", "最新市值", "参考市值")}
PNL = {_header(value) for value in ("盈亏", "浮动盈亏", "参考盈亏", "持仓盈亏")}

TOTAL_ASSET = {_header(value) for value in ("总资产", "资产总值", "资产总额")}
AVAILABLE_CASH = {_header(value) for value in ("可用资金", "可用金额", "资金可用")}
TOTAL_MARKET_VALUE = {_header(value) for value in ("股票市值", "证券市值", "持仓市值", "总市值")}


def normalize_symbol(value: object) -> str:
    raw = _text(value).upper()
    match = re.search(r"(?<!\d)(\d{6})(?:\.(SH|SZ|BJ))?(?!\d)", raw)
    if not match:
        raise BrokerExportError("BROKER_EXPORT_SYMBOL_INVALID")
    code, exchange = match.groups()
    if exchange is None:
        exchange = "SH" if code[0] in "569" else "BJ" if code.startswith(("4", "8", "92")) else "SZ"
    return f"{code}.{exchange}"


@dataclass(frozen=True)
class ParsedExport:
    rows: list[dict]
    ignored_rows: list[dict]
    encoding: str
    sha256: str
    header_row: int


def parse_holdings_export(path: Path) -> ParsedExport:
    raw_rows, encoding, digest = _decode_table(Path(path))
    header_row, columns = _find_header(raw_rows, (SYMBOL, NAME, QUANTITY, SELLABLE, PRICE, MARKET_VALUE))
    positions, ignored = [], []
    for line_number, row in enumerate(raw_rows[header_row + 1 :], start=header_row + 2):
        try:
            symbol = normalize_symbol(_cell(row, columns, SYMBOL))
        except BrokerExportError:
            ignored.append({"line": line_number, "reason": "not_a_position_row", "raw": row})
            continue
        quantity = _decimal(_cell(row, columns, QUANTITY), "quantity")
        record = {
            "symbol": symbol,
            "name": _text(_cell(row, columns, NAME)),
            "quantity": quantity,
            "sellable_quantity": _decimal(_cell(row, columns, SELLABLE), "sellable_quantity"),
            "average_cost": _decimal(_cell(row, columns, COST, required=False), "average_cost", required=False),
            "market_price": _decimal(_cell(row, columns, PRICE), "market_price"),
            "market_value": _decimal(_cell(row, columns, MARKET_VALUE), "market_value"),
            "unrealized_pnl": _decimal(_cell(row, columns, PNL, required=False), "unrealized_pnl", required=False, signed=True),
            "metadata": {"source_line": line_number, "source_kind": "ths_holdings_export"},
        }
        if quantity == 0:
            ignored.append({"line": line_number, "reason": "zero_actual_quantity", "raw": row})
        else:
            positions.append(record)
    if not positions and not ignored:
        raise BrokerExportError("BROKER_EXPORT_NO_POSITION_ROWS")
    if len({row["symbol"] for row in positions}) != len(positions):
        raise BrokerExportError("BROKER_EXPORT_DUPLICATE_POSITION")
    return ParsedExport(positions, ignored, encoding, digest, header_row + 1)


def _key_value_account(rows: list[list[str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    wanted = TOTAL_ASSET | AVAILABLE_CASH | TOTAL_MARKET_VALUE
    for row in rows[:30]:
        for offset, value in enumerate(row[:-1]):
            key = _header(value)
            if key in wanted and row[offset + 1]:
                result[key] = row[offset + 1]
    return result


def parse_account_export(path: Path) -> dict:
    rows, encoding, digest = _decode_table(Path(path))
    values = _key_value_account(rows)
    if not all(any(alias in values for alias in group) for group in (TOTAL_ASSET, AVAILABLE_CASH, TOTAL_MARKET_VALUE)):
        try:
            header_row, columns = _find_header(rows, (TOTAL_ASSET, AVAILABLE_CASH, TOTAL_MARKET_VALUE))
            data = next((row for row in rows[header_row + 1 :] if any(row)), None)
            if data is None:
                raise BrokerExportError("BROKER_EXPORT_ACCOUNT_ROW_MISSING")
            values = {key: data[offset] if offset < len(data) else "" for key, offset in columns.items()}
        except BrokerExportError as error:
            raise BrokerExportError("BROKER_EXPORT_ACCOUNT_TOTALS_MISSING") from error
    return {
        "total_asset": _decimal(next(values[a] for a in TOTAL_ASSET if a in values), "total_asset"),
        "cash": _decimal(next(values[a] for a in AVAILABLE_CASH if a in values), "available_cash"),
        "total_market_value": _decimal(next(values[a] for a in TOTAL_MARKET_VALUE if a in values), "total_market_value"),
        "encoding": encoding,
        "sha256": digest,
    }


TRADE_DATE = {_header(value) for value in ("交易日期", "发生日期", "成交日期")}
TRADE_TIME = {_header(value) for value in ("成交时间", "交易时间", "发生时间")}
TRADE_TYPE = {_header(value) for value in ("备注", "业务名称", "操作", "买卖标志")}
TRADE_QUANTITY = {_header(value) for value in ("成交数量", "发生数量", "数量")}
TRADE_PRICE = {_header(value) for value in ("成交价格", "成交均价", "价格")}
GROSS_AMOUNT = {_header(value) for value in ("成交金额", "交易金额")}
NET_AMOUNT = {_header(value) for value in ("发生金额", "清算金额", "净发生额")}
CURRENCY = {_header(value) for value in ("货币单位", "币种")}
FEE_GROUPS = {
    "commission": {_header(value) for value in ("手续费", "佣金")},
    "stamp_duty": {_header(value) for value in ("印花税",)},
    "transfer_fee": {_header(value) for value in ("过户费",)},
    "other_fee": {_header(value) for value in ("交易所清算费", "基金手续费", "规费", "其他费用")},
}

DAILY_SIDE = {_header("买卖")}
ORDER_NUMBER = {_header("委托编号")}
EXECUTION_NUMBER = {_header("成交编号")}
ORDER_TIME = {_header("委托时间")}


def _date(value: object) -> date:
    raw = re.sub(r"\D", "", _text(value))
    if len(raw) != 8:
        raise BrokerExportError("BROKER_EXPORT_TRADE_DATE_INVALID")
    return datetime.strptime(raw, "%Y%m%d").date()


def _time(value: object) -> time | None:
    raw = re.sub(r"\D", "", _text(value))
    if not raw:
        return None
    raw = raw.zfill(6)
    if len(raw) != 6:
        raise BrokerExportError("BROKER_EXPORT_TRADE_TIME_INVALID")
    return datetime.strptime(raw, "%H%M%S").time()


def parse_trade_export(path: Path) -> ParsedExport:
    raw_rows, encoding, digest = _decode_table(Path(path))
    header_row, columns = _find_header(raw_rows, (TRADE_DATE, SYMBOL, NAME, TRADE_TYPE, TRADE_QUANTITY))
    records, ignored, duplicate_ordinals = [], [], {}
    for line_number, row in enumerate(raw_rows[header_row + 1 :], start=header_row + 2):
        raw_type = _text(_cell(row, columns, TRADE_TYPE, required=False))
        if any(word in raw_type for word in ("撤单", "废单", "取消")):
            ignored.append({"line": line_number, "reason": "cancelled_or_invalid", "raw": row})
            continue
        side = "buy" if "买" in raw_type else "sell" if "卖" in raw_type else None
        if side is None:
            ignored.append({"line": line_number, "reason": "not_a_buy_or_sell", "raw": row})
            continue
        try:
            symbol = normalize_symbol(_cell(row, columns, SYMBOL))
            trade_date = _date(_cell(row, columns, TRADE_DATE))
        except BrokerExportError:
            ignored.append({"line": line_number, "reason": "not_a_trade_row", "raw": row})
            continue
        raw_map = {_text(raw_rows[header_row][offset]): (row[offset] if offset < len(row) else "")
                   for offset in range(len(raw_rows[header_row]))}
        canonical = json.dumps(raw_map, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        base = sha256(canonical.encode("utf-8")).hexdigest()
        duplicate_ordinals[base] = duplicate_ordinals.get(base, 0) + 1
        trade_key = sha256(f"ths:{base}:{duplicate_ordinals[base]}".encode()).hexdigest()
        fees = {}
        for name, aliases in FEE_GROUPS.items():
            values = [_decimal(row[offset], name, required=False, signed=True) for alias in aliases
                      for offset in [columns.get(alias)] if offset is not None and offset < len(row)]
            fees[name] = sum((abs(value) if value is not None else Decimal(0) for value in values), Decimal(0))
        records.append({
            "trade_key": trade_key,
            "trade_date": trade_date,
            "trade_time": _time(_cell(row, columns, TRADE_TIME, required=False)),
            "symbol": symbol,
            "name": _text(_cell(row, columns, NAME)),
            "side": side,
            "quantity": _decimal(_cell(row, columns, TRADE_QUANTITY), "trade_quantity"),
            "price": _decimal(_cell(row, columns, TRADE_PRICE, required=False), "trade_price", required=False),
            "gross_amount": _decimal(_cell(row, columns, GROSS_AMOUNT, required=False), "gross_amount", required=False),
            "net_amount": _decimal(_cell(row, columns, NET_AMOUNT, required=False), "net_amount", required=False, signed=True),
            "currency": _text(_cell(row, columns, CURRENCY, required=False)) or "人民币",
            **fees,
            "metadata": {"source_line": line_number, "raw_type": raw_type, "raw_row": raw_map},
        })
    if not records:
        raise BrokerExportError("BROKER_EXPORT_NO_TRADE_ROWS")
    return ParsedExport(records, ignored, encoding, digest, header_row + 1)


def parse_daily_execution_export(path: Path, trade_date: date) -> ParsedExport:
    """Parse date-less 当日成交 rows with exact fill time; zero rows are not cancellations."""
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise BrokerExportError("BROKER_DAILY_EXECUTION_DATE_REQUIRED")
    raw_rows, encoding, digest = _decode_table(Path(path))
    required = (TRADE_TIME, SYMBOL, NAME, DAILY_SIDE, TRADE_QUANTITY, TRADE_PRICE,
                GROSS_AMOUNT, ORDER_NUMBER, EXECUTION_NUMBER, ORDER_TIME)
    header_row, columns = _find_header(raw_rows, required)
    records, ignored = [], []
    seen: dict[str, str] = {}
    for line_number, row in enumerate(raw_rows[header_row + 1:], start=header_row + 2):
        quantity = _decimal(_cell(row, columns, TRADE_QUANTITY), "trade_quantity")
        if quantity == 0:
            ignored.append({"line": line_number, "reason": "zero_quantity_no_fill_status"})
            continue
        symbol = normalize_symbol(_cell(row, columns, SYMBOL))
        raw_side = _text(_cell(row, columns, DAILY_SIDE))
        side = "buy" if "买" in raw_side else "sell" if "卖" in raw_side else None
        if side is None:
            raise BrokerExportError("BROKER_DAILY_EXECUTION_SIDE_INVALID")
        trade_time = _time(_cell(row, columns, TRADE_TIME))
        order_time = _time(_cell(row, columns, ORDER_TIME))
        if trade_time is None or order_time is None:
            raise BrokerExportError("BROKER_DAILY_EXECUTION_TIME_MISSING")
        price = _decimal(_cell(row, columns, TRADE_PRICE), "trade_price")
        gross_amount = _decimal(_cell(row, columns, GROSS_AMOUNT), "gross_amount")
        if price <= 0 or gross_amount <= 0:
            raise BrokerExportError("BROKER_DAILY_EXECUTION_VALUE_INVALID")
        order_number = _text(_cell(row, columns, ORDER_NUMBER))
        execution_number = _text(_cell(row, columns, EXECUTION_NUMBER))
        if not order_number or not execution_number or execution_number == "0000":
            raise BrokerExportError("BROKER_DAILY_EXECUTION_ID_MISSING")
        raw_map = {_text(raw_rows[header_row][offset]): (row[offset] if offset < len(row) else "")
                   for offset in range(len(raw_rows[header_row])) if _text(raw_rows[header_row][offset])}
        canonical = json.dumps(raw_map, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        trade_key = sha256(f"ths-daily-fill:{trade_date}:{symbol}:{execution_number}".encode()).hexdigest()
        if trade_key in seen:
            if seen[trade_key] != canonical:
                raise BrokerExportError("BROKER_DAILY_EXECUTION_DUPLICATE_CONFLICT")
            ignored.append({"line": line_number, "reason": "duplicate_execution"})
            continue
        seen[trade_key] = canonical
        records.append({
            "trade_key": trade_key, "trade_date": trade_date, "trade_time": trade_time,
            "symbol": symbol, "name": _text(_cell(row, columns, NAME)), "side": side,
            "quantity": quantity, "price": price, "gross_amount": gross_amount,
            "net_amount": None, "currency": "人民币",
            "commission": Decimal(0), "stamp_duty": Decimal(0),
            "transfer_fee": Decimal(0), "other_fee": Decimal(0),
            "metadata": {"source_line": line_number, "raw_type": raw_side,
                         "order_number": order_number, "execution_number": execution_number,
                         "order_time": order_time.isoformat(), "raw_row": raw_map,
                         "fee_semantics": "not_provided"},
        })
    if not records:
        raise BrokerExportError("BROKER_DAILY_EXECUTION_NO_FILLS")
    return ParsedExport(records, ignored, encoding, digest, header_row + 1)


__all__ = [
    "BrokerExportError", "ParsedExport", "normalize_symbol", "parse_account_export",
    "parse_holdings_export", "parse_trade_export", "parse_daily_execution_export",
]
