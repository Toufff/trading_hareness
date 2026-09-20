"""LonghuVIP full-market evidence transport.

This module owns network and vendor-contract concerns only.  It deliberately
does not know about PostgreSQL.  Every vendor list request is hard capped at
300 records, matching the purchased account's verified physical request
limit; larger logical reads are paginated by the caller here.

``main_net`` is the vendor's order-size-classified field 13.  It is not
institution identity and it is not Level-2 cancellation/order-book evidence.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import requests

from .licensed_stock_api import execute as execute_licensed_stock_api
from .market_rules import cn_today, is_trading_day
from .symbols import canonical_symbol


MAX_PAGE_SIZE = 300
FLOW_CONVENTION = "longhuvip_zs_stocklist_main_net_field13"
DEFAULT_CONFIG_PATH = Path.home() / ".stock-brain" / "longhu_vendor.json"
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; V2178A Build/UP1A.231005.007)"


def safe_page_size(value: int) -> int:
    requested = int(value)
    if requested <= 0:
        raise ValueError("Longhu vendor page size must be positive")
    return min(requested, MAX_PAGE_SIZE)


def _number(value: Any) -> float | None:
    if value in (None, "", "-", "--") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize_stock_symbol(value: Any) -> str | None:
    """Resolve a vendor field to a canonical stock symbol, delegating the
    exchange/board inference to the shared ``app.symbols`` table.

    This used to take the trailing 6 digits of whatever string arrived and
    route by a single leading digit, which mapped ``sh000300`` (an index) to
    a nonexistent Shenzhen stock and could not tell a Shanghai B-share
    (``900xxx``) from a genuine Beijing Stock Exchange listing (``920xxx``).

    An explicit exchange prefix/suffix is trusted only when it agrees with
    the board the bare code itself implies; ``sh000300`` names an index
    (CSI 300), not a Shanghai stock at code 000300, so it is rejected rather
    than silently accepted as one.
    """
    raw = str(value or "").strip().upper()
    symbol = canonical_symbol(raw, kind="stock")
    if symbol is None:
        return None
    code, _board = symbol.split(".", 1)
    if canonical_symbol(code, kind="stock") != symbol:
        return None
    return symbol


def _stock_code(value: Any) -> str | None:
    symbol = normalize_stock_symbol(value)
    return symbol.split(".", 1)[0] if symbol else None


def _index_symbol(value: Any) -> str | None:
    """Accept only an explicitly exchange-qualified SSE 000xxx / SZSE 399xxx index."""
    raw = str(value or "").strip().upper()
    match = re.fullmatch(r"(?:(SH|SZ)(\d{6})|(\d{6})\.(SH|SZ))", raw)
    if not match:
        return None
    exchange, code = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
    return f"{code}.{exchange}" if (exchange, code[:3]) in {("SH", "000"), ("SZ", "399")} else None


def longhu_security_id(symbol: Any) -> tuple[str, str] | None:
    """Return ``(canonical symbol, vendor StockID)`` for a stock or an index.

    Longhu addresses stocks by bare code and indexes by ``SH000001`` /
    ``SZ399001``.  Only an exchange-qualified ``000xxx.SH`` / ``399xxx.SZ`` is
    an index; a bare ``000001`` stays Ping An Bank.
    """
    index = _index_symbol(symbol)
    if index:
        code, exchange = index.split(".")
        return index, f"{exchange}{code}"
    stock = normalize_stock_symbol(symbol)
    return (stock, stock.split(".", 1)[0]) if stock else None


def _vendor_day(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())[:8]


def _book_levels(weituo: Mapping[str, Any], prefix: str) -> list[dict[str, float]]:
    """Five price/size levels in board lots; an empty side stays explicit zeros."""
    levels = []
    for level in range(1, 6):
        pair = weituo.get(f"{prefix}{level}")
        valid = isinstance(pair, list) and len(pair) >= 2
        levels.append({"price": (_number(pair[0]) if valid else None) or 0.0,
                       "size": (_number(pair[1]) if valid else None) or 0.0})
    return levels


def parse_stock_snapshot_payload(payload: Mapping[str, Any], symbol: str) -> dict[str, Any] | None:
    """Normalize one ``GetStockPanKou`` response for the live watch pipeline.

    The vendor response exposes an exchange timestamp.  Keeping that timestamp
    separate from our receipt time lets the existing freshness gate reject a
    delayed response instead of treating a successful HTTP request as fresh.
    Volumes and depth sizes are board lots on every board, STAR included.
    """
    normalized = normalize_stock_symbol(symbol)
    code = _stock_code(symbol)
    real = payload.get("real") if isinstance(payload.get("real"), Mapping) else {}
    price = _number(real.get("last_px"))
    if not normalized or not code or _stock_code(payload.get("code")) != code or price is None or price <= 0:
        return None
    day = _vendor_day(payload.get("day"))
    quote_time = "".join(character for character in str(real.get("time") or "") if character.isdigit())[:6]
    weituo = payload.get("weituo") if isinstance(payload.get("weituo"), Mapping) else {}
    return {
        "ts_code": normalized,
        "name": str(payload.get("name") or normalized),
        "price": price,
        "pre_close": _number(payload.get("preclose_px")),
        "open": _number(real.get("open_px")),
        "high": _number(real.get("high_px")),
        "low": _number(real.get("low_px")),
        "pct_change": _number(real.get("px_change_rate")),
        "volume": _number(real.get("total_amount")),
        "amount": _number(real.get("total_turnover")),
        "vwap": _number(real.get("avg_px")),
        "turnover_rate": _number(real.get("turnover_ratio")),
        "volume_ratio": _number(real.get("vol_ratio")),
        "amplitude": _number(real.get("amplitude")),
        "pe_ttm": _number(real.get("TTMPeRate")),
        "pb": _number(real.get("dyn_pb_rate")),
        "trade_date": day or None,
        "trade_time": f"{day}{quote_time}" if len(day) == 8 and len(quote_time) == 6 else None,
        # ``amount_out`` tracks the outer (active-buy) side and ``amount_in``
        # the inner side.  Longhu's classifier is its own; do not treat it as
        # interchangeable with another feed's outer/inner split.
        "outer_volume_lot": _number(real.get("amount_out")),
        "inner_volume_lot": _number(real.get("amount_in")),
        "bids": _book_levels(weituo, "b"),
        "asks": _book_levels(weituo, "s"),
        "raw": {"provider": "longhuvip", "action": "GetStockPanKou"},
    }


def parse_stock_minute_payload(payload: Mapping[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Normalize per-minute Longhu rows (stock or index) without inventing Level-2 semantics.

    Trend rows are ``[HH:MM, price, session VWAP, minute volume (lots), flag]``.
    The VWAP column is cumulative, so cumulative amount is VWAP x cumulative
    lots x 100 and a minute's amount is the difference; this reconciles with
    the snapshot's ``total_turnover``.
    """
    resolved = longhu_security_id(symbol)
    if not resolved:
        return []
    normalized = resolved[0]
    day = _vendor_day(payload.get("day"))
    session_date = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else None
    rows: list[dict[str, Any]] = []
    cumulative_volume = 0.0
    previous_amount: float | None = 0.0
    for raw in payload.get("trend") or []:
        if not isinstance(raw, list) or len(raw) < 4:
            continue
        minute = str(raw[0] or "").replace(":", "")[:4]
        price = _number(raw[1])
        raw_volume = _number(raw[3])
        # A missing/unparseable volume field is not evidence of a genuine
        # zero-volume minute; coercing it to 0.0 previously fabricated
        # amount=price*0*100=0 as though that were an observed value.
        volume_missing = raw_volume is None
        volume_lot = max(0.0, raw_volume or 0.0)
        if not re.fullmatch(r"\d{4}", minute) or price is None or price <= 0:
            continue
        cumulative_volume += volume_lot
        average_price = _number(raw[2])
        cumulative_amount = (round(average_price * cumulative_volume * 100, 4)
                             if average_price is not None and average_price > 0 else None)
        amount = (None if volume_missing or cumulative_amount is None or previous_amount is None
                  else round(max(0.0, cumulative_amount - previous_amount), 4))
        previous_amount = cumulative_amount
        rows.append({
            "symbol": normalized,
            "ts_code": normalized,
            "session_date": session_date,
            "time": minute,
            "close": price,
            "vwap": average_price,
            "volume_lot": volume_lot,
            "vol": volume_lot,
            "amount": amount,
            "cumulative_volume_lot": cumulative_volume,
            "cumulative_amount": cumulative_amount,
            "cumulative_segment": 0 if minute <= "1130" else 1,
            "cumulative_reset": False,
            "is_complete": not volume_missing,
            "source": "longhuvip:GetStockTrendIncremental",
        })
    if rows:
        rows[-1]["is_complete"] = False
    return rows


def parse_daily_kline_payload(payload: Mapping[str, Any], symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """Unadjusted ``GetKLineDay_W14`` bars (``Is_FS=0``) inside ``[start, end]`` (YYYYMMDD).

    ``y`` rows are open/close/high/low.  ``vol`` is board lots and ``bal`` is
    CNY; rows leave here in the canonical lots / thousand-CNY contract.
    """
    resolved = longhu_security_id(symbol)
    dates, values = payload.get("x") or [], payload.get("y") or []
    if not resolved or len(dates) != len(values):
        return []
    volumes, amounts = payload.get("vol") or [], payload.get("bal") or []
    rows: list[dict[str, Any]] = []
    for index, (raw_day, candle) in enumerate(zip(dates, values)):
        day = _vendor_day(raw_day)
        if len(day) != 8 or not start <= day <= end or not isinstance(candle, list) or len(candle) < 4:
            continue
        open_, close, high, low = (_number(value) for value in candle[:4])
        if any(value is None or value <= 0 for value in (open_, close, high, low)):
            continue
        if not low <= min(open_, close) <= max(open_, close) <= high:
            continue
        previous = values[index - 1] if index > 0 and isinstance(values[index - 1], list) and len(values[index - 1]) > 1 else None
        amount_cny = _number(amounts[index]) if index < len(amounts) else None
        rows.append({
            "ts_code": resolved[0], "trade_date": day,
            "open": open_, "close": close, "high": high, "low": low,
            "pre_close": _number(previous[1]) if previous else None,
            "vol": _number(volumes[index]) if index < len(volumes) else None,
            "amount": amount_cny / 1000 if amount_cny is not None else None,
            "price_basis": "unadjusted", "source": "longhuvip:GetKLineDay_W14",
        })
    return rows


def fetch_daily_kline(source: "LonghuIntradaySource", symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    """Fetch dated unadjusted daily bars through the licensed call contract (local or gateway)."""
    resolved = longhu_security_id(symbol)
    if not resolved:
        raise ValueError(f"unsupported Longhu security: {symbol}")
    first = date(int(start[:4]), int(start[4:6]), int(start[6:]))
    size = min(MAX_PAGE_SIZE, max(20, (cn_today() - first).days + 10))
    envelope = source.raw_call({
        "target": "longhu_history", "path": "/w1/api/index.php",
        "params": {"a": "GetKLineDay_W14", "c": "StockLineData", "apiv": "w40", "StockID": resolved[1],
                   "Type": "d", "Is_FS": "0", "st": size, "Index": 0},
    })
    rows: list[dict[str, Any]] = []
    for page in envelope.get("pages") or []:
        payload = page.get("payload") if isinstance(page, Mapping) else None
        if not isinstance(payload, Mapping):
            continue
        if payload.get("errcode") is not None and str(payload.get("errcode")) != "0":
            raise RuntimeError(f"Longhu errcode={payload.get('errcode')} action=GetKLineDay_W14")
        rows.extend(parse_daily_kline_payload(payload, symbol, start, end))
    return rows


@dataclass(frozen=True)
class LonghuVendorConfig:
    token: str
    user_id: str
    device_id: str
    version: str = "5.20.0.2"
    ranking_device_id: str = "20ad85ca-becb-3bed-b3d4-30032a0f5923"
    page_size: int = MAX_PAGE_SIZE
    plate_page_size: int = 60
    timeout_seconds: float = 20.0
    retries: int = 3
    workers: int = 12

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "LonghuVendorConfig":
        result = cls(
            token=str(payload.get("token") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
            device_id=str(payload.get("device_id") or "").strip(),
            version=str(payload.get("version") or "5.20.0.2").strip(),
            ranking_device_id=str(payload.get("ranking_device_id") or cls.ranking_device_id),
            page_size=safe_page_size(int(payload.get("page_size") or MAX_PAGE_SIZE)),
            plate_page_size=safe_page_size(int(payload.get("plate_page_size") or 60)),
            timeout_seconds=float(payload.get("timeout_seconds") or 20.0),
            retries=max(1, int(payload.get("retries") or 3)),
            workers=max(1, min(24, int(payload.get("workers") or 12))),
        )
        if not result.token or not result.user_id or not result.device_id or not result.version:
            raise ValueError("Longhu token, user_id, device_id and version are required")
        return result

    @classmethod
    def load(cls, path: str | Path | None = None) -> "LonghuVendorConfig":
        resolved = Path(path or os.getenv("QUANT_LONGHU_CONFIG_PATH") or DEFAULT_CONFIG_PATH)
        return cls.from_mapping(json.loads(resolved.read_text(encoding="utf-8")))


def configured(path: str | Path | None = None) -> bool:
    if os.getenv("QUANT_SHARED_READ_API_BASE_URL", "").strip() and os.getenv(
        "QUANT_SHARED_READ_API_KEY", ""
    ).strip():
        return True
    try:
        LonghuVendorConfig.load(path)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


class LonghuIntradaySource(Protocol):
    """Small contract shared by the local licensed and remote gateway clients."""

    def watch_quotes(
        self, symbols: Iterable[str], *, max_symbols: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...

    def stock_minutes(self, symbol: str) -> list[dict[str, Any]]: ...

    def raw_call(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class SharedLonghuReadSource:
    """Read normalized licensed evidence through the owner's gateway.

    The upstream Longhu credential never leaves the owner's machine. A peer
    only receives normalized rows and cannot widen one logical request beyond
    the same 300-symbol ceiling enforced by the local adapter.
    """

    def __init__(
        self, base_url: str | None = None, read_key: str | None = None,
        *, timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = str(base_url or os.getenv("QUANT_SHARED_READ_API_BASE_URL") or "").rstrip("/")
        self.read_key = str(read_key or os.getenv("QUANT_SHARED_READ_API_KEY") or "").strip()
        if not self.base_url or not self.read_key:
            raise ValueError("shared Longhu base URL and read key are required")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._session_lock = threading.Lock()
        self._session = self._new_session()

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"X-Quant-Read-Key": self.read_key, "Accept": "application/json"})
        return session

    def _replace_stale_session(self, failed: requests.Session) -> requests.Session:
        # A reverse-tunnel restart leaves the peer's keep-alive socket looking
        # reusable until its first request. Every gateway operation is
        # read-only, including POST /licensed/stock-api/call, so retry exactly
        # once on a transport disconnect with a fresh pool. Do not retry HTTP
        # errors or timeouts: those carry different operational meaning.
        with self._session_lock:
            if self._session is failed:
                self._session = self._new_session()
                failed.close()
            return self._session

    def _get_response(self, path: str, *, params: Mapping[str, Any] | None = None) -> requests.Response:
        session = self._session
        try:
            return session.get(
                f"{self.base_url}{path}", params=dict(params or {}), timeout=self.timeout_seconds,
            )
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            return self._replace_stale_session(session).get(
                f"{self.base_url}{path}", params=dict(params or {}), timeout=self.timeout_seconds,
            )

    def _post_response(self, path: str, *, payload: Mapping[str, Any]) -> requests.Response:
        timeout = max(180.0, self.timeout_seconds)
        session = self._session
        try:
            return session.post(f"{self.base_url}{path}", json=dict(payload), timeout=timeout)
        except (requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            return self._replace_stale_session(session).post(
                f"{self.base_url}{path}", json=dict(payload), timeout=timeout,
            )

    def _get(self, path: str, *, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self._get_response(path, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError("shared Longhu gateway response must be an object")
        return payload

    def _post(self, path: str, *, payload: Mapping[str, Any]) -> dict[str, Any]:
        response = self._post_response(path, payload=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise TypeError("shared stock API response must be an object")
        return result

    def watch_quotes(
        self, symbols: Iterable[str], *, max_symbols: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ordered = list(dict.fromkeys(
            symbol for value in symbols if (symbol := normalize_stock_symbol(value)) is not None
        ))
        limit = len(ordered) if max_symbols is None else max(1, int(max_symbols))
        selected = ordered[:limit]
        if not selected:
            return [], {
                "status": "completed", "requested": 0, "selected": 0, "received": 0,
                "truncated": False, "max_symbols": limit, "errors": [],
                "source": "shared-longhu-gateway",
            }
        payload = self._get("/licensed/longhu/quotes", params={"symbols": ",".join(selected)})
        rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
        status = payload.get("source_status") if isinstance(payload.get("source_status"), dict) else {}
        return rows, {
            **status,
            "requested": len(ordered),
            "selected": len(selected),
            "received": len(rows),
            "truncated": len(ordered) > len(selected),
            "max_symbols": limit,
            "transport": "shared_gateway",
        }

    def stock_minutes(self, symbol: str) -> list[dict[str, Any]]:
        resolved = longhu_security_id(symbol)
        if not resolved:
            raise ValueError(f"unsupported Longhu security: {symbol}")
        normalized = resolved[0]
        payload = self._get(f"/licensed/longhu/minutes/{normalized}")
        rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
        if not rows:
            raise RuntimeError(f"shared Longhu minute returned no rows for {normalized}")
        return rows

    def raw_call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Forward the complete documented call contract to the owner gateway."""
        return self._post("/licensed/stock-api/call", payload=request)


def intraday_source() -> LonghuIntradaySource:
    """Prefer the shared gateway only when both endpoint and key are present."""
    if os.getenv("QUANT_SHARED_READ_API_BASE_URL", "").strip() and os.getenv(
        "QUANT_SHARED_READ_API_KEY", ""
    ).strip():
        return SharedLonghuReadSource()
    return LonghuVendorSource()


def parse_industry_stock_row(row: Any, trade_date: date, plate_id: str) -> dict[str, Any] | None:
    if not isinstance(row, list) or len(row) < 26:
        return None
    symbol = normalize_stock_symbol(row[0])
    close, main_net = _number(row[5]), _number(row[13])
    if not symbol or close is None or close <= 0 or main_net is None:
        return None
    return {
        "symbol": symbol,
        "trade_date": trade_date.strftime("%Y%m%d"),
        "name": str(row[1] or symbol),
        "close": close,
        "pct_chg": _number(row[6]),
        "amount": _number(row[7]),
        "main_net": main_net,
        "volume_ratio": _number(row[21]),
        "turnover_rate": _number(row[25]),
        "total_mv": _number(row[37]) if len(row) > 37 else None,
        "circ_mv": _number(row[38]) if len(row) > 38 else None,
        "pb": _number(row[53]) if len(row) > 53 else None,
        "pe": _number(row[61]) if len(row) > 61 else None,
        "plate_id": str(plate_id),
        "flow_convention": FLOW_CONVENTION,
        "raw": {"vendor_row": row, "plate_id": str(plate_id), "row_length": len(row)},
    }


class LonghuVendorSource:
    def __init__(self, config: LonghuVendorConfig | None = None) -> None:
        self.config = config or LonghuVendorConfig.load()
        # ``requests.Session`` is not documented thread-safe, but every
        # method here is dispatched across up to ``config.workers`` (24)
        # concurrent threads (``ThreadPoolExecutor`` in ``watch_quotes`` and
        # ``full_market_vendor_rows``). A thread-local session avoids
        # sharing that mutable connection-pool state across threads while
        # still reusing one TCP/TLS connection per worker thread.
        self._thread_local = threading.local()

    @property
    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,*/*"})
            self._thread_local.session = session
        return session

    def _credentials(self) -> dict[str, Any]:
        return {
            "PhoneOSNew": 1,
            "DeviceID": self.config.device_id,
            "VerSion": self.config.version,
            "Token": self.config.token,
            "UserID": self.config.user_id,
        }

    def _json(self, url: str, params: Mapping[str, Any], *, authenticate: bool = True) -> dict[str, Any]:
        merged = self._credentials() if authenticate else {"PhoneOSNew": 1}
        merged.update({key: value for key, value in params.items() if value is not None})
        if "st" in merged:
            merged["st"] = safe_page_size(int(merged["st"]))
        error: Exception | None = None
        for attempt in range(self.config.retries):
            try:
                response = self._session.get(url, params=merged, timeout=self.config.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError("Longhu top-level response must be an object")
                if payload.get("errcode") is not None and str(payload.get("errcode")) != "0":
                    raise RuntimeError(f"Longhu errcode={payload.get('errcode')} action={merged.get('a')}")
                return payload
            except Exception as caught:  # upstream transport has heterogeneous errors
                error = caught
                if attempt + 1 < self.config.retries:
                    time.sleep(0.35 * (attempt + 1))
        assert error is not None
        raise error

    def raw_call(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Call any documented stock endpoint through the owner-held identity."""
        batch = request.get("batch") if isinstance(request.get("batch"), Mapping) else {}
        return execute_licensed_stock_api(
            session=self._session,
            config=self.config,
            target_key=str(request.get("target") or ""),
            path=str(request["path"]) if request.get("path") is not None else None,
            params=request.get("params") if isinstance(request.get("params"), Mapping) else {},
            batch_param=str(batch.get("param")) if batch.get("param") else None,
            batch_values=batch.get("values") if isinstance(batch.get("values"), list) else (),
            batch_separator=str(batch.get("separator") or ","),
        )

    def stock_quote(self, symbol: str) -> dict[str, Any]:
        """Fetch one exchange-timestamped quote from the licensed endpoint."""
        code = _stock_code(symbol)
        if not code:
            raise ValueError(f"unsupported Longhu stock symbol: {symbol}")
        payload = self._json(
            "https://apphwhq.longhuvip.com/w1/api/index.php",
            {"a": "GetStockPanKou", "c": "StockL2Data", "apiv": "w41", "StockID": code},
        )
        parsed = parse_stock_snapshot_payload(payload, symbol)
        if parsed is None:
            raise RuntimeError(f"Longhu quote missing or mismatched for {code}")
        return parsed

    def watch_quotes(self, symbols: Iterable[str], *, max_symbols: int | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch an explicit watch basket (never an implicit all-A scan); ``max_symbols`` is optional."""
        ordered = list(dict.fromkeys(
            symbol for value in symbols if (symbol := normalize_stock_symbol(value)) is not None
        ))
        limit = len(ordered) if max_symbols is None else max(1, int(max_symbols))
        selected = ordered[:limit]
        rows: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(
            max_workers=min(self.config.workers, len(selected) or 1), thread_name_prefix="longhu-watch",
        ) as pool:
            futures = {pool.submit(self.stock_quote, symbol): symbol for symbol in selected}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    rows[symbol] = future.result()
                except Exception as error:  # one symbol must not abort the basket
                    errors.append(f"{symbol}:{type(error).__name__}:{error}")
        return [rows[symbol] for symbol in selected if symbol in rows], {
            "status": "completed" if len(rows) == len(selected) else "partial" if rows else "failed",
            "requested": len(ordered),
            "selected": len(selected),
            "received": len(rows),
            "truncated": len(ordered) > len(selected),
            "max_symbols": limit,
            "errors": errors[:20],
            "source": "longhuvip:GetStockPanKou",
        }

    def stock_minutes(self, symbol: str) -> list[dict[str, Any]]:
        """Fetch the current session minute path for one explicit stock or index."""
        resolved = longhu_security_id(symbol)
        if not resolved:
            raise ValueError(f"unsupported Longhu security: {symbol}")
        payload = self._json(
            "https://apphwhq.longhuvip.com/w1/api/index.php",
            {
                "a": "GetStockTrendIncremental", "c": "StockL2Data", "apiv": "w41",
                "Type": 1, "StockID": resolved[1],
            },
        )
        rows = parse_stock_minute_payload(payload, resolved[0])
        if not rows:
            raise RuntimeError(f"Longhu minute returned no rows for {resolved[1]}")
        return rows

    def industry_plate_catalog(self) -> list[dict[str, Any]]:
        url = "https://apphq.longhuvip.com/w1/api/index.php"
        offset, result, seen = 0, [], set()
        for _ in range(10):
            payload = self._json(url, {
                "Order": 1, "a": "RealRankingInfo", "st": self.config.plate_page_size,
                "apiv": "w26", "Type": 1, "c": "ZhiShuRanking",
                "DeviceID": self.config.ranking_device_id, "Index": offset, "ZSType": 4,
            }, authenticate=False)
            rows = payload.get("list") or []
            for row in rows:
                plate = str(row[0] if isinstance(row, list) and row else "").strip()
                if plate and plate not in seen:
                    seen.add(plate)
                    result.append({
                        "sector_key": plate,
                        "label": str(row[1] if len(row) > 1 else plate),
                        "strength": _number(row[2]) if len(row) > 2 else None,
                        "change_pct": _number(row[3]) if len(row) > 3 else None,
                        "speed": _number(row[4]) if len(row) > 4 else None,
                        "amount": _number(row[5]) if len(row) > 5 else None,
                        "net_inflow": _number(row[6]) if len(row) > 6 else None,
                        "volume_ratio": _number(row[9]) if len(row) > 9 else None,
                        "taxonomy_key": "longhu_ths_industry",
                    })
            total = int(_number(payload.get("Count")) or 0)
            if not rows or len(rows) < self.config.plate_page_size or total and len(result) >= total:
                break
            offset += len(rows)
        if len(result) < 90:
            raise RuntimeError(f"Longhu industry coverage too small: {len(result)}")
        return result

    def industry_plates(self) -> list[str]:
        return [row["sector_key"] for row in self.industry_plate_catalog()]

    def plate_day(self, plate_id: str, trade_date: date, *, live: bool) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0
        for _ in range(10):
            host = ("https://apphwshhq.longhuvip.com/w1/api/index.php" if live
                    else "https://apphis.longhuvip.com/w1/api/index.php")
            params: dict[str, Any] = {
                "Order": 1, "a": "ZhiShuStockList_W8", "st": self.config.page_size,
                "c": "ZhiShuRanking", "old": 1, "IsZZ": 0, "Index": offset,
                "REnd": 1500, "apiv": "w41", "Type": 6, "IsKZZType": 0,
                "PlateID": plate_id, "TSZB_Type": 0, "filterType": 0,
            }
            params["RStart" if live else "Date"] = "0925" if live else trade_date.isoformat()
            payload = self._json(host, params)
            rows = payload.get("list") or []
            for row in rows:
                parsed = parse_industry_stock_row(row, trade_date, plate_id)
                if parsed and parsed["symbol"] not in seen:
                    seen.add(parsed["symbol"])
                    result.append(parsed)
            total = int(_number(payload.get("Count")) or 0)
            if not rows or len(rows) < self.config.page_size or total and len(result) >= total:
                break
            offset += len(rows)
        return result

    def full_market_vendor_rows(
        self, trade_date: date, *, plate_ids: Iterable[str] | None = None, now: Any = None,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """Fetch and merge every industry plate's cross-section for one date.

        ``live`` selects the exchange's real-time host and is intentionally
        conservative: a container's local clock is UTC, so comparing against
        ``date.today()`` treated 00:00-08:00 Beijing time as still "yesterday"
        and a weekend/holiday as though it were a live session, silently
        stamping the prior trading day's closed-market snapshot with today's
        date.  ``cn_today()`` fixes the timezone; ``is_trading_day`` refuses
        to call a non-trading day "live" at all.
        """
        plates = list(plate_ids) if plate_ids is not None else self.industry_plates()
        live = trade_date == cn_today(now) and is_trading_day(trade_date)
        by_symbol: dict[str, dict[str, Any]] = {}
        conflicts: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        successful = 0
        with ThreadPoolExecutor(max_workers=min(self.config.workers, len(plates)), thread_name_prefix="longhu-market") as pool:
            futures = {pool.submit(self.plate_day, plate, trade_date, live=live): plate for plate in plates}
            for future in as_completed(futures):
                plate = futures[future]
                try:
                    rows = future.result()
                    successful += 1
                    for row in rows:
                        existing = by_symbol.get(row["symbol"])
                        if existing and existing["main_net"] != row["main_net"]:
                            conflicts.append({"symbol": row["symbol"], "plates": [existing["plate_id"], plate]})
                            continue
                        by_symbol[row["symbol"]] = row
                except Exception as error:
                    errors.append({"plate_id": plate, "error": f"{type(error).__name__}: {error}"})
        health = {
            "plates": len(plates), "successful_plates": successful,
            "plate_coverage": successful / len(plates) if plates else 0.0,
            "symbols": len(by_symbol), "errors": errors[:20],
            "duplicate_conflicts": conflicts[:20], "physical_page_limit": MAX_PAGE_SIZE,
        }
        return by_symbol, health

    def fetch_full_market_evidence(
        self, trade_date: date, extra_symbols: Iterable[str] = (),
    ) -> dict[str, Any]:
        """One session's plate cross-section plus dated licensed OHLC.

        ``extra_symbols`` is the authoritative equity universe.  The industry
        plates are the vendor's *classification*, not its listing roster, and
        using them as the request roster silently bounded the market to
        whatever the vendor had classified: on 2026-09-18 that was 90 of 345
        BSE names, so the other 255 were never requested at all.  The licensed
        per-symbol kline answers for them normally -- this only asks.
        """
        catalog = self.industry_plate_catalog()
        vendor, vendor_health = self.full_market_vendor_rows(
            trade_date, plate_ids=[row["sector_key"] for row in catalog],
        )
        from .longhu_settled_quotes import fetch as fetch_settled_quotes
        off_plate = [symbol for symbol in dict.fromkeys(extra_symbols) if symbol not in vendor]
        requested = list(vendor) + off_plate
        quotes, quote_health = fetch_settled_quotes(self, requested, trade_date, workers=8)
        quote_health["plate_symbols"] = len(vendor)
        quote_health["off_plate_requested"] = len(off_plate)
        members_by_plate: dict[str, list[dict[str, Any]]] = {}
        for row in vendor.values():
            members_by_plate.setdefault(str(row["plate_id"]), []).append(row)
        board_rows: list[dict[str, Any]] = []
        from .longhu_board_close import aggregate_board
        for board in catalog:
            members = members_by_plate.get(board["sector_key"], [])
            board_rows.append(aggregate_board(board, members, trade_date))
        return {
            "trade_date": trade_date, "vendor_rows": vendor, "quote_rows": quotes,
            "board_rows": board_rows,
            "health": {"longhu": vendor_health, "licensed_ohlc": quote_health},
        }


__all__ = [
    "DEFAULT_CONFIG_PATH", "FLOW_CONVENTION", "LonghuIntradaySource", "LonghuVendorConfig",
    "LonghuVendorSource", "SharedLonghuReadSource", "intraday_source",
    "MAX_PAGE_SIZE", "configured", "fetch_daily_kline", "longhu_security_id", "normalize_stock_symbol",
    "parse_daily_kline_payload", "parse_industry_stock_row", "parse_stock_minute_payload",
    "parse_stock_snapshot_payload", "safe_page_size",
]
