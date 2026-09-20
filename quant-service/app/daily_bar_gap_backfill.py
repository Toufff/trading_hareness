"""Fill-only backfill of missing A-share daily bars from licensed longhu history.

Why this exists
---------------
``quant.canonical_bars_daily`` had no A-share rows for 2026-04-15 .. 2026-06-30
(51 sessions).  The history was copied into the owner database from the peer
in two bulk transfers on 2026-09-18: one for 2026-07-01 .. 08-31 and one for
everything *before the owner's earliest stored date*.  That earliest date was
2026-04-15 -- not an A-share date at all, but the first overseas
``legacy:yahoo_chart`` row the stock-brain import had put into the same table
on 2026-09-10 -- so the second copy stopped at 2026-04-14 and nothing ever
covered the window in between.

Source (longhu only, user rule)
-------------------------------
The licensed daily kline (``GetKLineDay_W14``) is FORWARD-adjusted and rounded
to the tick, so an unadjusted price cannot be recovered from it exactly: a
cash dividend with a half-cent per-share amount (0.185) makes every value a
rounding tie, and bonus shares map two raw ticks onto one adjusted tick.
Validated on 93,594 boundary bars, pure inversion was off by a tick on 4,450
of them and by more where the vendor's ``CQ`` list is incomplete.

The same vendor's historical L2 snapshot, ``GetStockPanKou`` on controller
``StockL2History`` with ``Day=YYYYMMDD``, returns the exchange's own values
for that session: pre_close, open, high, low, last, volume (lots),
turnover (CNY) and the limit band.  Against 41,898 stored tushare bars of
2026-04-08..04-14 and 07-01..07-08 it matched open/high/low/close/pre_close
exactly in every case, volume and amount in all but 13 Beijing rows (the
vendor's longhu kline agrees with the snapshot there), and the limit band on
every non-Beijing row.  Beijing limits are computed from pre_close with the
exchange's inward rounding (up floored, down ceiled), which matched tushare
on all 3,458 Beijing rows; the vendor's own Beijing band is a tick off on
575 of them.

Indexes are not in the snapshot's scope here; their bars come from the same
licensed kline the platform's ``longhuvip_index`` lane already uses (indexes
have no corporate actions, so that series is unadjusted).

What is written
---------------
Only rows that do not exist yet (``ON CONFLICT DO NOTHING`` everywhere; an
existing canonical row is never replaced, whatever its provider):

* ``quant.raw_market_observations`` -- one evidence row per bar, with the
  vendor snapshot and the method in ``payload``;
* ``quant.market_bars_daily`` and ``quant.canonical_bars_daily``;
* one ``quant.fetch_runs`` receipt per session.

``available_at`` is the moment the vendor answered (never back-dated).
``adj_factor`` is the stored promotable cumulative factor for that
symbol/date (the tushare cross-section already present in
``quant.daily_adjustment_factors``), chosen with the same preference the
factor lane uses and admitted by ``promotable_adjustment_factor``; a bar
without one stays NULL.  ``quant.instruments`` is only registered for a
symbol that does not exist (``ensure_instruments``, ``DO NOTHING``) --
existing instrument rows are not touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .daily_bar_repository import daily_amount_unit_mismatch
from .instrument_registry import ensure_instruments
from .longhu_adjustment_factors import (
    MANUAL_METHOD_PREFIX, PROVIDER_PREFERENCE_SQL, ex_reference_price, parse_corporate_action,
)
from .longhu_vendor_source import longhu_security_id, normalize_stock_symbol, parse_daily_kline_payload
from .market_rules import a_share_limit_prices
from .request_models import DailyBar
from .tushare_normalization import promotable_adjustment_factor, promotable_factor_evidence_sql_param

#: Selected-provider name of a stock bar written here: the real source, and
#: distinct from the evening close lane (``longhuvip_composite``) so the rows
#: stay identifiable.  The method is in every evidence payload.
PROVIDER_KEY = "longhuvip_l2history_pankou"
#: Index bars use the platform's existing longhu index lane name: same source
#: (the licensed kline), same unadjusted index series.
INDEX_PROVIDER_KEY = "longhuvip_index"
SOURCE = "longhuvip:StockL2History.GetStockPanKou"
INDEX_SOURCE = "longhuvip:GetKLineDay_W14"
METHOD = "longhu_l2history_pankou_fill_v1"
AVAILABILITY_BASIS = "vendor_fetched_at_backfill_v1"
CAPABILITY = "daily_bar"

TICK = Decimal("0.01")
#: A session is written only when at least this share of the symbols the
#: vendor traded that day produced a valid bar.
MIN_SESSION_COVERAGE = 0.95

_A_SHARE_PATTERN = r"^[0-9]{6}\.(SH|SZ|BJ)$"


# --------------------------------------------------------------------------
# Vendor requests (the platform's licensed call contract; no auth here)
# --------------------------------------------------------------------------

def pankou_history_request(code: str, day: str) -> dict[str, Any]:
    return {"target": "longhu_history", "path": "/w1/api/index.php",
            "params": {"a": "GetStockPanKou", "c": "StockL2History", "apiv": "w41",
                       "StockID": code, "Day": day}}


def kline_request(code: str, sessions: int = 300) -> dict[str, Any]:
    return {"target": "longhu_history", "path": "/w1/api/index.php",
            "params": {"a": "GetKLineDay_W14", "c": "StockLineData", "apiv": "w40", "StockID": code,
                       "Type": "d", "Is_FS": "0", "st": int(sessions), "Index": 0}}


#: Snapshot fields kept in the evidence cache and payload (the order book is
#: dropped: it is the vendor's closing book, not part of a daily bar).
PANKOU_KEEP = ("time", "last_px", "high_px", "low_px", "open_px", "avg_px", "total_amount",
               "total_turnover", "up_px", "down_px", "phcj_volume", "phcj_turnover",
               "turnover_ratio", "total_shares", "circulation_amount")


def _payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    pages = envelope.get("pages") or []
    payload = pages[0].get("payload") if pages and isinstance(pages[0], Mapping) else None
    payload = payload if isinstance(payload, Mapping) else {}
    if payload.get("errcode") not in (None, "0", 0):
        raise RuntimeError(f"Longhu errcode={payload.get('errcode')}")
    return dict(payload)


def fetch_pankou_record(source: Any, symbol: str, day: str) -> dict[str, Any]:
    """One dated snapshot, reduced to the evidence record the cache stores."""
    resolved = longhu_security_id(symbol)
    if not resolved:
        raise ValueError(f"unsupported Longhu security: {symbol}")
    requested = datetime.now(timezone.utc)
    payload = _payload(source.raw_call(pankou_history_request(resolved[1], day)))
    received = datetime.now(timezone.utc)
    real = payload.get("real") if isinstance(payload.get("real"), Mapping) else {}
    return {
        "symbol": symbol, "day": day,
        "requested_at": requested.isoformat(), "received_at": received.isoformat(),
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "vendor_day": payload.get("day"), "code": payload.get("code"),
        "preclose_px": payload.get("preclose_px"), "status": payload.get("status"),
        "sourcetype": payload.get("sourcetype"), "ts": payload.get("ts"),
        "real": {key: real.get(key) for key in PANKOU_KEEP if key in real},
    }


def fetch_kline_record(source: Any, symbol: str, sessions: int = 300) -> dict[str, Any]:
    resolved = longhu_security_id(symbol)
    if not resolved:
        raise ValueError(f"unsupported Longhu security: {symbol}")
    requested = datetime.now(timezone.utc)
    envelope = source.raw_call(kline_request(resolved[1], sessions))
    received = datetime.now(timezone.utc)
    pages = [page.get("payload") for page in envelope.get("pages") or [] if isinstance(page, Mapping)]
    for page in pages:
        if isinstance(page, Mapping) and page.get("errcode") not in (None, "0", 0):
            raise RuntimeError(f"Longhu errcode={page.get('errcode')}")
    return {"symbol": symbol, "requested_at": requested.isoformat(), "received_at": received.isoformat(),
            "pages": pages}


# --------------------------------------------------------------------------
# Evidence cache: the fetch is resumable and the plan is reproducible
# --------------------------------------------------------------------------

class EvidenceStore:
    """``<root>/kline/<symbol>.json`` and ``<root>/pankou/<YYYYMMDD>.jsonl``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "kline").mkdir(parents=True, exist_ok=True)
        (self.root / "pankou").mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def kline_path(self, symbol: str) -> Path:
        return self.root / "kline" / f"{symbol}.json"

    def has_kline(self, symbol: str) -> bool:
        return self.kline_path(symbol).exists()

    def put_kline(self, record: Mapping[str, Any]) -> None:
        path = self.kline_path(str(record["symbol"]))
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def kline(self, symbol: str) -> dict[str, Any] | None:
        path = self.kline_path(symbol)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def pankou_path(self, day: str) -> Path:
        return self.root / "pankou" / f"{day}.jsonl"

    def pankou_day(self, day: str) -> dict[str, dict[str, Any]]:
        """Last record per symbol (a re-fetch appends; the newest wins)."""
        path = self.pankou_path(day)
        records: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a torn last line from an interrupted run
                    records[str(record.get("symbol"))] = record
        return records

    def append_pankou(self, records: Iterable[Mapping[str, Any]]) -> None:
        by_day: dict[str, list[Mapping[str, Any]]] = {}
        for record in records:
            by_day.setdefault(str(record["day"]), []).append(record)
        with self._lock:
            for day, rows in by_day.items():
                with self.pankou_path(day).open("a", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def fetch_parallel(
    work: Sequence[Any], call: Any, *, workers: int = 16, retries: int = 4,
    retry_pause_seconds: float = 5.0, on_result: Any = None,
) -> dict[Any, str]:
    """Run ``call(item)`` for every item, retrying failures in later rounds.

    The same shape as the factor lane's fetch: bounded parallelism, no
    artificial throttle, an occasional HTTP 503 retried after a pause.
    Returns ``{item: error}`` for what still failed.
    """
    pending = list(work)
    errors: dict[Any, str] = {}
    for attempt in range(max(1, retries + 1)):
        if not pending:
            break
        if attempt:
            time.sleep(retry_pause_seconds)
        failed: list[Any] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending))),
                                thread_name_prefix="bars-gap-backfill") as pool:
            futures = {pool.submit(call, item): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                except Exception as error:  # noqa: BLE001 - one item must not end the run
                    errors[item] = f"{type(error).__name__}: {str(error)[:160]}"
                    failed.append(item)
                    continue
                errors.pop(item, None)
                if on_result is not None:
                    on_result(item, result)
        pending = failed
    return errors


# --------------------------------------------------------------------------
# Pure parsing
# --------------------------------------------------------------------------

def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", "-", "--") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def is_beijing(symbol: str) -> bool:
    return symbol.endswith(".BJ")


def limit_prices(symbol: str, pre_close: Decimal, up_px: Any, down_px: Any) -> tuple[
        Decimal | None, Decimal | None, str]:
    """``(limit_up, limit_down, basis)`` for one stock session.

    A zero band from the vendor is a session without a price limit (the first
    sessions of a new listing); it is stored as NULL, never as a sentinel.
    """
    up, down = _decimal(up_px), _decimal(down_px)
    if not up or not down or up <= 0 or down <= 0:
        return None, None, "vendor_reports_no_limit"
    if is_beijing(symbol):
        limit_up, limit_down = a_share_limit_prices(symbol, pre_close)
        return limit_up, limit_down, "beijing_pre_close_inward_rounding"
    return up.quantize(TICK), down.quantize(TICK), "vendor_l2history_up_down_px"


@dataclass(frozen=True)
class HistoryBar:
    symbol: str
    trading_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    limit_up: Decimal | None
    limit_down: Decimal | None
    limit_basis: str
    fetched_at: datetime
    provider: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    flags: tuple[str, ...] = ()


def parse_pankou_record(record: Mapping[str, Any], symbol: str, trading_date: date) -> tuple[
        HistoryBar | None, str]:
    """A bar, or ``(None, reason)`` when the snapshot is not a traded session."""
    day = trading_date.strftime("%Y%m%d")
    if str(record.get("day")) != day or str(record.get("vendor_day") or "") != day:
        return None, "vendor_day_mismatch"
    resolved = longhu_security_id(symbol)
    if not resolved or str(record.get("code") or "").upper() not in {resolved[1].upper(), symbol.split(".")[0]}:
        return None, "vendor_code_mismatch"
    real = record.get("real") if isinstance(record.get("real"), Mapping) else {}
    prices = [_decimal(real.get(key)) for key in ("open_px", "high_px", "low_px", "last_px")]
    pre_close = _decimal(record.get("preclose_px"))
    volume, turnover = _decimal(real.get("total_amount")), _decimal(real.get("total_turnover"))
    if not volume or not turnover or volume <= 0 or turnover <= 0:
        return None, "no_trade"
    if any(value is None or value <= 0 for value in prices) or pre_close is None or pre_close <= 0:
        return None, "invalid_price"
    open_, high, low, close = (value.quantize(TICK) for value in prices)  # type: ignore[union-attr]
    if not low <= min(open_, close) <= max(open_, close) <= high:
        return None, "inconsistent_ohlc"
    pre_close = pre_close.quantize(TICK)
    limit_up, limit_down, basis = limit_prices(symbol, pre_close, real.get("up_px"), real.get("down_px"))
    fetched = datetime.fromisoformat(str(record["received_at"]))
    return HistoryBar(
        symbol=symbol, trading_date=trading_date, open=open_, high=high, low=low, close=close,
        pre_close=pre_close, volume=volume, amount=turnover / 1000, limit_up=limit_up,
        limit_down=limit_down, limit_basis=basis, fetched_at=fetched, provider=PROVIDER_KEY,
        evidence={"source": SOURCE, "method": METHOD, "day": day,
                  "payload_sha256": record.get("payload_sha256"),
                  "requested_at": record.get("requested_at"), "received_at": record.get("received_at"),
                  "preclose_px": record.get("preclose_px"), "status": record.get("status"),
                  "sourcetype": record.get("sourcetype"), "vendor_ts": record.get("ts"),
                  "real": dict(real), "limit_basis": basis,
                  "units": {"volume": "lots (vendor total_amount)",
                            "amount": "thousand CNY (vendor total_turnover / 1000)"}},
    ), "ok"


@dataclass(frozen=True)
class KlineDay:
    trading_date: date
    qfq: tuple[float, float, float, float]      # open, high, low, close as served
    cq: str | None


def parse_kline(record: Mapping[str, Any] | None) -> dict[date, KlineDay]:
    days: dict[date, KlineDay] = {}
    if not record:
        return days
    for payload in record.get("pages") or []:
        if not isinstance(payload, Mapping):
            continue
        dates, values = payload.get("x") or [], payload.get("y") or []
        actions = payload.get("CQ") or []
        if len(dates) != len(values):
            continue
        for index, (raw_day, candle) in enumerate(zip(dates, values)):
            digits = "".join(ch for ch in str(raw_day) if ch.isdigit())[:8]
            if len(digits) != 8 or not isinstance(candle, list) or len(candle) < 4:
                continue
            try:
                trading_date = date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
                open_, close, high, low = (float(value) for value in candle[:4])
            except (TypeError, ValueError):
                continue
            cq = actions[index] if isinstance(actions, list) and index < len(actions) else None
            days[trading_date] = KlineDay(trading_date, (open_, high, low, close),
                                          str(cq).strip() or None if cq is not None else None)
    return days


def forward_adjust(raw: Decimal, actions: Sequence[Any]) -> Decimal:
    """The vendor's subtractive forward adjustment, before its rounding."""
    value = raw
    for action in actions:
        shares = Decimal(repr(action.shares_per10)) / 10
        rights = Decimal(repr(action.rights_per10)) / 10
        value = ((value - Decimal(repr(action.cash_per10)) / 10 + Decimal(repr(action.rights_price)) * rights)
                 / (1 + shares + rights))
    return value


def kline_agrees(bar: HistoryBar, kline: Mapping[date, KlineDay]) -> bool | None:
    """Whether the vendor's forward-adjusted candle is a rounding of this bar.

    ``None`` when the kline has no candle for the day.  Disagreement is
    reported, not fatal: the kline's own corporate-action list has gaps
    (000563.SZ, 001331.SZ: an adjustment with no ``CQ`` record), and the
    snapshot matched the exchange's stored bars exactly where the kline did not.
    """
    day = kline.get(bar.trading_date)
    if day is None:
        return None
    actions = [action for value in sorted(kline) if value > bar.trading_date
               and (action := parse_corporate_action(kline[value].cq)) is not None]
    half = Decimal("0.005") + Decimal("1e-7")
    for raw, served in zip((bar.open, bar.high, bar.low, bar.close), day.qfq):
        if abs(forward_adjust(raw, actions) - Decimal(repr(served)).quantize(TICK)) > half:
            return False
    return True


def index_bars_from_kline(record: Mapping[str, Any] | None, symbol: str, start: date, end: date) -> list[HistoryBar]:
    """Index bars from the licensed kline, exactly as the platform's index lane reads them."""
    if not record:
        return []
    fetched = datetime.fromisoformat(str(record["received_at"]))
    bars: list[HistoryBar] = []
    for payload in record.get("pages") or []:
        if not isinstance(payload, Mapping):
            continue
        for row in parse_daily_kline_payload(payload, symbol, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")):
            day = row["trade_date"]
            values = [_decimal(row.get(key)) for key in ("open", "high", "low", "close")]
            if any(value is None for value in values):
                continue
            volume = _decimal(row.get("vol"))
            amount = _decimal(row.get("amount"))
            bars.append(HistoryBar(
                symbol=symbol, trading_date=date(int(day[:4]), int(day[4:6]), int(day[6:])),
                open=values[0], high=values[1], low=values[2], close=values[3],  # type: ignore[arg-type]
                pre_close=_decimal(row.get("pre_close")), volume=volume, amount=amount,
                limit_up=None, limit_down=None, limit_basis="index_has_no_limit",
                fetched_at=fetched, provider=INDEX_PROVIDER_KEY,
                evidence={"source": INDEX_SOURCE, "method": METHOD, "requested_at": record.get("requested_at"),
                          "received_at": record.get("received_at"),
                          "semantics": "index kline; indexes carry no corporate actions",
                          "vendor_row": {key: row.get(key) for key in ("open", "high", "low", "close",
                                                                      "pre_close", "vol", "amount")}},
            ))
    return bars


# --------------------------------------------------------------------------
# Continuity (report, not a gate: the snapshot pre_close is the exchange's)
# --------------------------------------------------------------------------

def continuity_flags(
    bars: Sequence[HistoryBar], kline: Mapping[date, KlineDay], *,
    anchor_close: Decimal | None, anchor_date: date | None,
    next_pre_close: Decimal | None, next_date: date | None,
) -> dict[date, tuple[str, ...]]:
    """Per bar: does its pre_close equal the previous close, or is it an ex-date?

    ``anchor_*`` is the stored bar before the window, ``next_*`` the stored bar
    after it (its pre_close must equal the window's last close unless it is an
    ex-date itself -- keyed on ``next_date``).
    """
    flags: dict[date, tuple[str, ...]] = {}
    ordered = sorted(bars, key=lambda bar: bar.trading_date)
    previous_close, previous_date = anchor_close, anchor_date
    for bar in ordered:
        found: list[str] = []
        action = parse_corporate_action(kline[bar.trading_date].cq) if bar.trading_date in kline else None
        if previous_close is not None and bar.pre_close is not None:
            if action is not None:
                reference = Decimal(repr(ex_reference_price(float(previous_close), action)))
                found.append("ex_date")
                if abs(reference - bar.pre_close) > Decimal("0.0101"):
                    found.append("ex_reference_disagrees")
            elif bar.pre_close != previous_close:
                found.append("pre_close_break_without_cq")
        elif previous_close is None:
            found.append("no_previous_close")
        flags[bar.trading_date] = tuple(found)
        previous_close, previous_date = bar.close, bar.trading_date
    if ordered and next_pre_close is not None and next_date is not None:
        action = parse_corporate_action(kline[next_date].cq) if next_date in kline else None
        if action is None and next_pre_close != ordered[-1].close:
            last = ordered[-1].trading_date
            flags[last] = (*flags.get(last, ()), "next_stored_pre_close_disagrees")
    return flags


# --------------------------------------------------------------------------
# Database reads
# --------------------------------------------------------------------------

OPEN_SESSIONS_SQL = """SELECT calendar_date FROM quant.market_trade_calendar
 WHERE exchange='SSE' AND is_open AND calendar_date BETWEEN %s AND %s ORDER BY 1"""

EXISTING_KEYS_SQL = f"""SELECT symbol, trading_date FROM quant.canonical_bars_daily
 WHERE trading_date BETWEEN %s AND %s AND symbol ~ '{_A_SHARE_PATTERN}'"""

#: Symbols to consider: every A-share code with a stored bar in the sessions
#: around the window, plus every code with factor evidence inside it.
UNIVERSE_SQL = f"""SELECT DISTINCT symbol FROM quant.canonical_bars_daily
 WHERE symbol ~ '{_A_SHARE_PATTERN}' AND trading_date BETWEEN %(before)s AND %(after)s
UNION
SELECT DISTINCT symbol FROM quant.daily_adjustment_factors
 WHERE symbol ~ '{_A_SHARE_PATTERN}' AND trading_date BETWEEN %(from_date)s AND %(to_date)s
ORDER BY 1"""

#: The stored promotable factor per symbol on one date, preferred exactly as
#: the factor lane prefers a checkpoint (manual row, then provider route).
FACTORS_SQL = f"""SELECT DISTINCT ON (factor.symbol, factor.trading_date)
       factor.symbol, factor.trading_date, factor.adj_factor, factor.provider, factor.raw
  FROM quant.daily_adjustment_factors factor
 WHERE factor.trading_date BETWEEN %s AND %s AND factor.adj_factor > 0
   AND {promotable_factor_evidence_sql_param('factor', 'raw')}
 ORDER BY factor.symbol, factor.trading_date,
          (coalesce(factor.raw->>'method','') LIKE '{MANUAL_METHOD_PREFIX}%%') DESC,
          {PROVIDER_PREFERENCE_SQL.format(alias='factor')}, factor.available_at DESC"""

ANCHOR_SQL = f"""SELECT DISTINCT ON (symbol) symbol, trading_date, close FROM quant.canonical_bars_daily
 WHERE trading_date < %s AND trading_date >= %s AND symbol ~ '{_A_SHARE_PATTERN}' AND close > 0
 ORDER BY symbol, trading_date DESC"""

NEXT_SQL = f"""SELECT DISTINCT ON (symbol) symbol, trading_date, pre_close FROM quant.canonical_bars_daily
 WHERE trading_date > %s AND trading_date <= %s AND symbol ~ '{_A_SHARE_PATTERN}'
 ORDER BY symbol, trading_date"""


def read_factors(connection: Any, from_date: date, to_date: date) -> dict[tuple[str, date], tuple[Decimal, str]]:
    """``{(symbol, date): (value, provider)}`` -- each admitted by the per-row promotion rule too."""
    result: dict[tuple[str, date], tuple[Decimal, str]] = {}
    for row in connection.execute(FACTORS_SQL, (from_date, to_date)).fetchall():
        raw = row["raw"] if isinstance(row["raw"], Mapping) else {}
        if not promotable_adjustment_factor(dict(raw), provider_key=str(row["provider"])):
            continue
        result[(row["symbol"], row["trading_date"])] = (Decimal(str(row["adj_factor"])), str(row["provider"]))
    return result


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def is_index(symbol: str) -> bool:
    return normalize_stock_symbol(symbol) != symbol and longhu_security_id(symbol) is not None


def in_scope(symbol: str) -> bool:
    """A listed stock or an exchange index.  Funds and other six-digit codes
    (a handful of legacy ETF rows share the table) are not part of the gap."""
    return normalize_stock_symbol(symbol) == symbol or is_index(symbol)


@dataclass
class Plan:
    from_date: date
    to_date: date
    sessions: list[date]
    rows: dict[date, list[DailyBar]]
    evidence: dict[tuple[str, date], HistoryBar]
    held: dict[str, list[tuple[str, str]]]            # reason -> [(symbol, date)]
    flags: dict[str, list[tuple[str, str]]]           # flag -> [(symbol, date)]
    stats: dict[str, Any]


def build_plan(
    connection: Any, store: EvidenceStore, from_date: date, to_date: date, *,
    boundary_lookback_days: int = 30,
) -> Plan:
    """Everything that would be written, from the evidence cache and the database."""
    from datetime import timedelta

    sessions = [row["calendar_date"] for row in connection.execute(OPEN_SESSIONS_SQL, (from_date, to_date)).fetchall()]
    session_set = set(sessions)
    existing = {(row["symbol"], row["trading_date"]) for row in connection.execute(
        EXISTING_KEYS_SQL, (from_date, to_date)).fetchall()}
    universe = [row["symbol"] for row in connection.execute(UNIVERSE_SQL, {
        "before": from_date - timedelta(days=boundary_lookback_days),
        "after": to_date + timedelta(days=boundary_lookback_days),
        "from_date": from_date, "to_date": to_date}).fetchall() if in_scope(row["symbol"])]
    factors = read_factors(connection, from_date, to_date)
    anchors = {row["symbol"]: (row["trading_date"], Decimal(str(row["close"]))) for row in connection.execute(
        ANCHOR_SQL, (from_date, from_date - timedelta(days=boundary_lookback_days * 3))).fetchall()}
    nexts = {row["symbol"]: (row["trading_date"], None if row["pre_close"] is None else Decimal(str(row["pre_close"])))
             for row in connection.execute(NEXT_SQL, (to_date, to_date + timedelta(days=boundary_lookback_days))).fetchall()}

    pankou = {value: store.pankou_day(value.strftime("%Y%m%d")) for value in sessions}
    held: dict[str, list[tuple[str, str]]] = {}
    flags: dict[str, list[tuple[str, str]]] = {}
    evidence: dict[tuple[str, date], HistoryBar] = {}
    stats: dict[str, Any] = {"universe": len(universe), "sessions": len(sessions),
                             "existing_rows_in_window": len(existing), "kline_missing": [],
                             "kline_agrees": 0, "kline_disagrees": 0}

    def hold(reason: str, symbol: str, value: date) -> None:
        held.setdefault(reason, []).append((symbol, str(value)))

    def flag(name: str, symbol: str, value: date) -> None:
        flags.setdefault(name, []).append((symbol, str(value)))

    bars_by_symbol: dict[str, list[HistoryBar]] = {}
    for symbol in universe:
        record = store.kline(symbol)
        if record is None:
            stats["kline_missing"].append(symbol)
            continue
        if is_index(symbol):
            bars_by_symbol[symbol] = [bar for bar in index_bars_from_kline(record, symbol, from_date, to_date)
                                      if bar.trading_date in session_set]
            continue
        kline = parse_kline(record)
        candles = [value for value in sorted(kline) if value in session_set]
        bars: list[HistoryBar] = []
        for value in candles:
            if (symbol, value) in existing:
                # Already stored, so ``fetch`` deliberately never asked for its
                # snapshot.  Classify it as what it is.  Filed under
                # ``pankou_not_fetched`` it counted as a *lost* symbol in
                # ``session_coverage``, and filling holes in a session that is
                # mostly present then scored ~0.08 and was refused by the 0.95
                # gate -- a coverage failure invented by the bookkeeping.
                hold("canonical_row_exists", symbol, value)
                continue
            snapshot = pankou[value].get(symbol)
            if snapshot is None:
                hold("pankou_not_fetched", symbol, value)
                continue
            bar, reason = parse_pankou_record(snapshot, symbol, value)
            if bar is None:
                hold(f"pankou_{reason}", symbol, value)
                continue
            agrees = kline_agrees(bar, kline)
            if agrees is False:
                stats["kline_disagrees"] += 1
                flag("kline_qfq_disagrees", symbol, value)
            elif agrees:
                stats["kline_agrees"] += 1
            bars.append(bar)
        anchor = anchors.get(symbol)
        following = nexts.get(symbol)
        for value, found in continuity_flags(
                bars, kline, anchor_close=anchor[1] if anchor else None, anchor_date=anchor[0] if anchor else None,
                next_pre_close=following[1] if following else None,
                next_date=following[0] if following else None).items():
            for name in found:
                flag(name, symbol, value)
        bars_by_symbol[symbol] = bars

    rows: dict[date, list[DailyBar]] = {value: [] for value in sessions}
    for symbol, bars in bars_by_symbol.items():
        for bar in bars:
            key = (symbol, bar.trading_date)
            if key in existing:
                hold("canonical_row_exists", symbol, bar.trading_date)
                continue
            factor = factors.get(key)
            if factor is None and not is_index(symbol):
                flag("no_stored_factor", symbol, bar.trading_date)
            try:
                daily = DailyBar(
                    symbol=symbol, trading_date=bar.trading_date, close=bar.close, open=bar.open,
                    high=bar.high, low=bar.low, pre_close=bar.pre_close, volume=bar.volume, amount=bar.amount,
                    adj_factor=factor[0] if factor else None, is_suspended=False,
                    limit_up=bar.limit_up, limit_down=bar.limit_down, source=bar.provider,
                    available_at=bar.fetched_at,
                )
            except ValueError:
                hold("daily_bar_validation", symbol, bar.trading_date)
                continue
            rows[bar.trading_date].append(daily)
            evidence[key] = bar
    stats["rows"] = {str(value): len(items) for value, items in rows.items()}
    stats["held"] = {reason: len(items) for reason, items in held.items()}
    stats["flags"] = {name: len(items) for name, items in flags.items()}
    stats["factor_rows"] = sum(1 for items in rows.values() for bar in items if bar.adj_factor is not None)
    return Plan(from_date, to_date, sessions, rows, evidence, held, flags, stats)


def session_coverage(plan: Plan, value: date) -> float:
    """Share of the vendor's traded stocks that produced a writable bar."""
    stocks = [bar for bar in plan.rows[value] if not is_index(bar.symbol)]
    lost = sum(1 for reason, items in plan.held.items() if reason != "canonical_row_exists"
               for _symbol, day in items if day == str(value))
    total = len(stocks) + lost
    return len(stocks) / total if total else 0.0


# --------------------------------------------------------------------------
# Fill-only persistence (one short transaction per session)
# --------------------------------------------------------------------------

def _normalized(bar: DailyBar) -> tuple[dict[str, Any], str]:
    """The same normalized document and hash ``upsert_daily_bar`` stores."""
    normalized = bar.model_dump(mode="json")
    return normalized, hashlib.sha256(repr(sorted(normalized.items())).encode("utf-8")).hexdigest()


def _utc(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("a backfilled bar must carry its fetch time")
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def persist_session(
    connection: Any, trading_date: date, bars: Sequence[DailyBar],
    evidence: Mapping[tuple[str, date], HistoryBar], *, run_id: str,
) -> dict[str, int]:
    """Insert the session's missing rows; never update or delete anything.

    Every statement is ``ON CONFLICT DO NOTHING``: a canonical or market row
    that appeared since the plan was built stays exactly as it is, and the
    count of rows actually inserted is returned.
    """
    ordered = sorted(bars, key=lambda bar: bar.symbol)
    if not ordered:
        return {"canonical": 0, "market": 0, "observations": 0, "instruments_checked": 0}
    ensure_instruments(connection, [bar.symbol for bar in ordered], PROVIDER_KEY)
    request_key = f"bars-gap-backfill:{run_id}:{trading_date.isoformat()}"
    run = connection.execute(
        """INSERT INTO quant.fetch_runs(provider_key,capability,market,trade_date,request_key,status,
               attempt_count,row_count,started_at,finished_at,metadata)
           VALUES(%s,%s,'cn',%s,%s,'running',1,0,now(),NULL,%s)
           ON CONFLICT(request_key) DO UPDATE SET attempt_count=quant.fetch_runs.attempt_count+1
           RETURNING fetch_run_id""",
        (PROVIDER_KEY, CAPABILITY, trading_date, request_key, json.dumps({
            "run_id": run_id, "method": METHOD, "source": SOURCE, "index_source": INDEX_SOURCE,
            "fill_only": True, "planned_rows": len(ordered)})),
    ).fetchone()
    fetch_run_id = run["fetch_run_id"]

    providers, symbols, effective, available, hashes, normalized_json, payload_json = [], [], [], [], [], [], []
    for bar in ordered:
        normalized, digest = _normalized(bar)
        source = evidence.get((bar.symbol, bar.trading_date))
        providers.append(bar.source)
        symbols.append(bar.symbol)
        effective.append(datetime.combine(bar.trading_date, datetime.min.time(), tzinfo=timezone.utc))
        available.append(_utc(bar.available_at))
        hashes.append(digest)
        normalized_json.append(json.dumps(normalized, ensure_ascii=False, sort_keys=True))
        payload_json.append(json.dumps({
            "normalized": normalized, "vendor": dict(source.evidence) if source else {},
            "provider": bar.source, "method": METHOD, "run_id": run_id, "fill_only": True,
            "adj_factor_source": "quant.daily_adjustment_factors (stored promotable row)"
                                 if bar.adj_factor is not None else None,
        }, ensure_ascii=False, sort_keys=True, default=str))
    connection.execute(
        """INSERT INTO quant.raw_market_observations(provider_key,capability,market,symbol,effective_at,
                  available_at,ingested_at,availability_basis,payload_sha256,normalized,payload,fetch_run_id)
           SELECT t.provider_key,%s,'cn',t.symbol,t.effective_at,t.available_at,now(),%s,t.payload_sha256,
                  t.normalized_json::jsonb,t.payload_json::jsonb,%s
             FROM unnest(%s::text[],%s::text[],%s::timestamptz[],%s::timestamptz[],%s::text[],%s::text[],%s::text[])
                  AS t(provider_key,symbol,effective_at,available_at,payload_sha256,normalized_json,payload_json)
            ORDER BY t.symbol
           ON CONFLICT(provider_key,capability,market,symbol,effective_at,payload_sha256) DO NOTHING""",
        (CAPABILITY, AVAILABILITY_BASIS, fetch_run_id,
         providers, symbols, effective, available, hashes, normalized_json, payload_json),
    )
    # A separate statement: it sees this transaction's inserts and any
    # identical evidence row that already existed (the conflict case).
    observation_rows = connection.execute(
        """SELECT o.symbol, o.observation_id FROM quant.raw_market_observations o
            JOIN unnest(%s::text[],%s::text[],%s::timestamptz[],%s::text[]) AS i(provider_key,symbol,effective_at,sha)
              ON o.provider_key=i.provider_key AND o.capability=%s AND o.market='cn'
             AND o.symbol=i.symbol AND o.effective_at=i.effective_at AND o.payload_sha256=i.sha""",
        (providers, symbols, effective, hashes, CAPABILITY),
    ).fetchall()
    observation_by_symbol = {row["symbol"]: str(row["observation_id"]) for row in observation_rows}

    mismatch = [daily_amount_unit_mismatch(source=bar.source, amount=bar.amount, volume=bar.volume, close=bar.close)
                for bar in ordered]
    amounts = [None if bad else bar.amount for bar, bad in zip(ordered, mismatch)]
    columns = (
        symbols, [bar.trading_date for bar in ordered], [bar.open for bar in ordered],
        [bar.high for bar in ordered], [bar.low for bar in ordered], [bar.close for bar in ordered],
        [bar.pre_close for bar in ordered], [bar.volume for bar in ordered], amounts,
        [bar.adj_factor for bar in ordered], [bool(bar.is_suspended) for bar in ordered],
        [bar.limit_up for bar in ordered], [bar.limit_down for bar in ordered],
    )
    market = connection.execute(
        """INSERT INTO quant.market_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,
               adj_factor,is_suspended,limit_up,limit_down,source,available_at)
           SELECT * FROM unnest(%s::text[],%s::date[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],
                                %s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::boolean[],
                                %s::numeric[],%s::numeric[],%s::text[],%s::timestamptz[])
            ORDER BY 1,2
           ON CONFLICT(symbol,trading_date) DO NOTHING
           RETURNING symbol""",
        (*columns, providers, available),
    ).fetchall()
    canonical = connection.execute(
        """INSERT INTO quant.canonical_bars_daily(symbol,trading_date,open,high,low,close,pre_close,volume,amount,
               adj_factor,is_suspended,limit_up,limit_down,selected_provider,source_observation_ids,
               quality_status,available_at)
           SELECT t.symbol,t.trading_date,t.open,t.high,t.low,t.close,t.pre_close,t.volume,t.amount,t.adj_factor,
                  t.is_suspended,t.limit_up,t.limit_down,t.provider,ARRAY[t.observation_id::uuid],t.quality,
                  t.available_at
             FROM unnest(%s::text[],%s::date[],%s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],
                         %s::numeric[],%s::numeric[],%s::numeric[],%s::numeric[],%s::boolean[],
                         %s::numeric[],%s::numeric[],%s::text[],%s::text[],%s::text[],%s::timestamptz[])
                  AS t(symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,
                       limit_up,limit_down,provider,observation_id,quality,available_at)
            ORDER BY 1,2
           ON CONFLICT(symbol,trading_date) DO NOTHING
           RETURNING symbol""",
        (*columns, providers, [observation_by_symbol[bar.symbol] for bar in ordered],
         ["partial" if bad else "fresh" for bad in mismatch], available),
    ).fetchall()
    issues = [(bar, bar.amount / (bar.volume * bar.close)) for bar, bad in zip(ordered, mismatch) if bad]
    for bar, ratio in issues:
        connection.execute(
            """INSERT INTO quant.data_quality_issues(capability,symbol,trading_date,severity,code,message,details)
               SELECT 'daily_bar',%s,%s,'warning','daily_amount_unit_mismatch',
                      'daily amount does not match the Tushare lots/thousand-yuan contract',%s::jsonb
                WHERE NOT EXISTS (SELECT 1 FROM quant.data_quality_issues
                                   WHERE capability='daily_bar' AND symbol=%s AND trading_date=%s
                                     AND code='daily_amount_unit_mismatch' AND resolved_at IS NULL)""",
            (bar.symbol, bar.trading_date, json.dumps({
                "provider": bar.source, "amount": str(bar.amount), "volume_lot": str(bar.volume),
                "close": str(bar.close), "implied_amount_per_lot_close": str(ratio),
                "action": "amount_quarantined_not_rescaled"}), bar.symbol, bar.trading_date),
        )
    connection.execute(
        """UPDATE quant.fetch_runs SET status='completed', row_count=%s, finished_at=now(),
                  metadata=metadata || %s::jsonb WHERE fetch_run_id=%s""",
        (len(canonical), json.dumps({"inserted_canonical": len(canonical), "inserted_market": len(market),
                                     "amount_quarantined": len(issues)}), fetch_run_id),
    )
    return {"canonical": len(canonical), "market": len(market), "observations": len(observation_by_symbol),
            "amount_quarantined": len(issues), "planned": len(ordered)}


__all__ = [
    "AVAILABILITY_BASIS", "EvidenceStore", "HistoryBar", "INDEX_PROVIDER_KEY", "KlineDay", "METHOD",
    "MIN_SESSION_COVERAGE", "PROVIDER_KEY", "Plan", "SOURCE", "beijing_limit_prices", "build_plan",
    "continuity_flags", "fetch_kline_record", "fetch_pankou_record", "fetch_parallel", "forward_adjust",
    "in_scope", "index_bars_from_kline", "is_index", "kline_agrees", "limit_prices", "pankou_history_request",
    "parse_kline", "parse_pankou_record", "persist_session", "read_factors", "session_coverage",
]
