"""Small, token-free public-market adapters used as corroborating sources.

These endpoints are intentionally limited to a single symbol and short daily
window.  They are not a historical backfill channel and are kept separate from
the licensed Tushare-compatible providers.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
import os
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .http_clients import public_http_client
from .http_retry import retry_delay_seconds
from .network_health import network_state
from .fuyao_provider import configured as fuyao_configured


class FreeProviderError(RuntimeError):
    """A concise, credential-free public-provider failure."""


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Retry only transient public HTTP failures once, without widening scope."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = await client.request(method, url, **kwargs)
            source = f"public:{url.split('/', 3)[2] if '://' in url else 'unknown'}"
            if 200 <= response.status_code < 400:
                network_state.record_success(source)
            elif response.status_code == 429 or response.status_code >= 500:
                network_state.record_failure(source, f"HTTP {response.status_code}", transient=True)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 0:
                    await asyncio.sleep(retry_delay_seconds(response.headers, 0.4))
                    continue
                response.raise_for_status()
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.TransportError) as error:
            network_state.record_failure(f"public:{url.split('/', 3)[2] if '://' in url else 'unknown'}", str(error), transient=True)
            last_error = error
            if attempt == 0:
                await asyncio.sleep(retry_delay_seconds(None, 0.4))
                continue
            break
    raise FreeProviderError(f"public HTTP {method.upper()} request failed after bounded retry") from last_error


def free_provider_status() -> list[dict[str, str | bool]]:
    akshare_configured = False
    if os.getenv("AKSHARE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            import akshare  # noqa: F401
            akshare_configured = True
        except Exception:
            akshare_configured = False
    xinhua_url = bool((os.getenv("XINHUA_FINANCE_API_URL") or "").strip())
    xinhua_credential = bool((os.getenv("XINHUA_FINANCE_API_KEY") or "").strip())
    has_fuyao = fuyao_configured()
    return [
        {"name": "eastmoney", "provider_key": "eastmoney_free", "label": "东方财富公开行情", "configured": True, "protocol": "public_http"},
        {"name": "tencent", "provider_key": "tencent_free", "label": "腾讯财经前复权日线（研究参考）", "configured": True,
         "protocol": "public_http", "canonical_promotion": False},
        {"name": "sina", "provider_key": "sina_free", "label": "新浪财经公开报价", "configured": True, "protocol": "public_http"},
        {"name": "cninfo", "provider_key": "cninfo_free", "label": "巨潮资讯公开公告", "configured": True, "protocol": "public_http"},
        {"name": "akshare", "provider_key": "akshare", "label": "AKShare 公开聚合源", "configured": akshare_configured, "protocol": "python_optional"},
        {"name": "fuyao_ths", "provider_key": "fuyao_ths", "label": "同花顺 Fuyao 数据服务", "configured": has_fuyao,
         "protocol": "x_api_key", "canonical_promotion": False},
        {"name": "xinhua_finance", "provider_key": "xinhua_finance", "label": "新华财经机构数据/API 流",
         "configured": xinhua_url and xinhua_credential, "protocol": "contract_required"},
    ]


def eastmoney_secid(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{1 if exchange == 'SH' else 0}.{code}"


def tencent_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{'sh' if exchange == 'SH' else 'sz' if exchange == 'SZ' else 'bj'}{code}"


def cninfo_stock_param(symbol: str) -> dict[str, str]:
    """Build the public CNInfo announcement selector for SH/SZ equities."""
    code, exchange = symbol.upper().split(".", 1)
    if exchange == "SH":
        return {"column": "sse", "plate": "sh", "stock": f"{code},gssh0{code}"}
    if exchange == "SZ":
        return {"column": "szse", "plate": "sz", "stock": f"{code},gssz0{code}"}
    return {"column": "third", "plate": "bj", "stock": code}


def classify_announcement_title(title: str) -> str:
    text = title.lower()
    if any(marker in title for marker in ("业绩", "年报", "半年报", "季报", "利润", "财务")):
        return "financial_report"
    if any(marker in title for marker in ("减持", "增持", "回购", "质押", "解禁", "股东")):
        return "shareholder_event"
    if any(marker in title for marker in ("问询", "监管", "处罚", "立案", "诉讼", "仲裁")):
        return "risk_event"
    if any(marker in title for marker in ("停牌", "复牌", "重大事项", "重组", "收购", "投资")):
        return "corporate_action"
    if "st" in text or "退市" in title:
        return "listing_risk"
    return "announcement"


async def cninfo_announcements(symbol: str, start: date, end: date, *, page_size: int = 30, max_pages: int = 3) -> list[dict[str, Any]]:
    if end < start:
        raise ValueError("end must not be before start")
    if (end - start).days > 90:
        raise ValueError("CNInfo announcement window is capped at 90 days")
    selector = cninfo_stock_param(symbol)
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Origin": "https://www.cninfo.com.cn",
        "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "User-Agent": "Mozilla/5.0",
    }
    rows: list[dict[str, Any]] = []
    async with public_http_client() as client:
        for page in range(1, max_pages + 1):
            data = {
                "pageNum": str(page), "pageSize": str(page_size), "tabName": "fulltext",
                "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}", "searchkey": "", "secid": "",
                "category": "", "trade": "", "sortName": "", "sortType": "", "isHLtitle": "true",
                **selector,
            }
            response = await _request_with_retry(client, "POST", "https://www.cninfo.com.cn/new/hisAnnouncement/query", data=data, headers=headers, timeout=15)
            payload = response.json()
            announcements = payload.get("announcements") or []
            if not isinstance(announcements, list):
                raise FreeProviderError("CNInfo returned an invalid announcement payload")
            for item in announcements:
                if not isinstance(item, dict):
                    continue
                timestamp = item.get("announcementTime")
                published_at = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc) if timestamp else None
                title = str(item.get("announcementTitle") or item.get("shortTitle") or "").replace("<em>", "").replace("</em>", "")
                url = str(item.get("adjunctUrl") or "")
                rows.append({
                    "ts_code": symbol, "sec_code": item.get("secCode"), "sec_name": item.get("secName") or item.get("tileSecName"),
                    "announcement_id": item.get("announcementId"), "title": title, "short_title": item.get("shortTitle"),
                    "event_type": classify_announcement_title(title), "published_at": published_at.isoformat() if published_at else None,
                    "announcement_date": published_at.date().isoformat() if published_at else None,
                    "url": f"https://static.cninfo.com.cn/{url}" if url and not url.startswith("http") else url,
                    "adjunct_type": item.get("adjunctType"), "adjunct_size": item.get("adjunctSize"), "raw": item,
                })
            if not payload.get("hasMore"):
                break
    return rows


async def eastmoney_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    params = {
        "secid": eastmoney_secid(symbol), "klt": "101", "fqt": "0", "beg": start, "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    async with public_http_client() as client:
        response = await _request_with_retry(client, "GET", "https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    payload = response.json()
    klines = (payload.get("data") or {}).get("klines") or []
    if not isinstance(klines, list):
        raise FreeProviderError("Eastmoney returned an invalid kline payload")
    rows: list[dict[str, Any]] = []
    for line in klines:
        values = str(line).split(",")
        if len(values) < 7:
            continue
        rows.append({"ts_code": symbol, "trade_date": values[0].replace("-", ""), "open": values[1], "close": values[2],
                     "high": values[3], "low": values[4], "vol": values[5], "amount": values[6], "pct_chg": values[8] if len(values) > 8 else None})
    return rows


async def eastmoney_quote(symbol: str) -> dict[str, Any] | None:
    params = {"secid": eastmoney_secid(symbol), "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f169,f170"}
    async with public_http_client() as client:
        response = await _request_with_retry(client, "GET", "https://push2.eastmoney.com/api/qt/stock/get", params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    data = (response.json().get("data") or {})
    if not data or data.get("f43") in (None, "-"):
        return None
    price = lambda key: float(data[key]) / 100 if data.get(key) not in (None, "-") else None
    return {"ts_code": symbol, "name": data.get("f58"), "close": price("f43"), "high": price("f44"), "low": price("f45"),
            "open": price("f46"), "pre_close": price("f60"), "vol": data.get("f47"), "amount": data.get("f48"),
            "change": price("f169"), "pct_chg": price("f170")}


async def eastmoney_watch_flow_quotes(symbols: list[str], *, max_symbols: int = 40) -> list[dict[str, Any]]:
    """Return one bounded Eastmoney watch-basket flow snapshot.

    This is deliberately *not* an all-A replacement: it supplies current
    volume ratio, turnover and indicative main flow for explicitly watched
    names when Tencent's slow all-A cross-section misses its scan budget.
    Callers must not derive cross-sectional percentiles from this small basket.
    """
    normalized = [symbol.upper() for symbol in symbols if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol.upper())]
    ordered = list(dict.fromkeys(normalized))[:max(1, max_symbols)]
    if not ordered:
        return []
    by_code = {symbol[:6]: symbol for symbol in ordered}
    params = {
        "fltt": "2", "invt": "2", "fields": "f2,f3,f8,f10,f12,f14,f62,f184",
        "secids": ",".join(eastmoney_secid(symbol) for symbol in ordered),
    }
    async with public_http_client() as client:
        response = await _request_with_retry(
            client, "GET", "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
    payload = response.json()
    rows = ((payload.get("data") or {}).get("diff") or [])
    if not isinstance(rows, list):
        raise FreeProviderError("Eastmoney returned an invalid watch-flow payload")
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        symbol = by_code.get(str(item.get("f12") or "").zfill(6))
        if not symbol:
            continue
        result.append({
            "ts_code": symbol, "name": item.get("f14"), "price": item.get("f2"),
            "pct_change": item.get("f3"), "turnover_rate": item.get("f8"),
            "volume_ratio": item.get("f10"), "main_net_inflow": item.get("f62"),
            "main_net_inflow_ratio": item.get("f184"), "raw": dict(item),
        })
    return result


async def tencent_daily(symbol: str, start: str, end: str) -> list[dict[str, Any]]:
    # Tencent's public endpoint accepts a bounded count. Filter locally by the
    # caller's allowed dates because its explicit date parameters are not
    # consistently honoured across securities.  These are explicitly qfq
    # (front-adjusted) rows and the caller persists them only as raw research
    # evidence; they must not enter the unadjusted canonical daily series.
    key = tencent_symbol(symbol)
    params = {"param": f"{key},day,,,80,qfq"}
    async with public_http_client() as client:
        response = await _request_with_retry(client, "GET", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    payload = response.json()
    if payload.get("code") != 0:
        raise FreeProviderError(str(payload.get("msg") or "Tencent kline request failed"))
    daily = ((payload.get("data") or {}).get(key) or {}).get("qfqday") or []
    if not isinstance(daily, list):
        raise FreeProviderError("Tencent returned an invalid kline payload")
    return [
        {"ts_code": symbol, "trade_date": values[0].replace("-", ""), "open": values[1], "close": values[2],
         "high": values[3], "low": values[4], "vol": values[5], "amount": None}
        for values in daily if isinstance(values, list) and len(values) >= 6 and start <= values[0].replace("-", "") <= end
    ]


async def sina_quote(symbol: str) -> dict[str, Any] | None:
    key = tencent_symbol(symbol)
    async with public_http_client() as client:
        response = await _request_with_retry(client, "GET", f"https://hq.sinajs.cn/list={key}", headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}, timeout=8)
    try:
        payload = response.text.split('"', 2)[1].split(",")
    except IndexError as error:
        raise FreeProviderError("Sina returned an invalid quote payload") from error
    if len(payload) < 10 or not payload[0]:
        return None
    return {"ts_code": symbol, "name": payload[0], "open": payload[1], "pre_close": payload[2], "close": payload[3],
            "high": payload[4], "low": payload[5], "vol": payload[8], "amount": payload[9],
            "trade_date": payload[30] if len(payload) > 30 else None, "trade_time": payload[31] if len(payload) > 31 else None}


def parse_sina_quote_batch(payload: str, symbols_by_key: dict[str, str]) -> list[dict[str, Any]]:
    """Parse Sina's multi-symbol quote response without treating blanks as prices."""
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r'var hq_str_([a-z0-9]+)="([^"]*)";', payload):
        symbol = symbols_by_key.get(match.group(1).lower())
        values = match.group(2).split(",")
        if not symbol or len(values) < 10 or not values[0]:
            continue
        rows.append({
            "ts_code": symbol, "name": values[0], "open": values[1], "pre_close": values[2], "close": values[3],
            "high": values[4], "low": values[5], "vol": values[8], "amount": values[9],
            "trade_date": values[30] if len(values) > 30 else None,
            "trade_time": values[31] if len(values) > 31 else None,
        })
    return rows


def tencent_minute_amount_scale(*, price: float, cumulative_volume_lot: int,
                                cumulative_amount: float) -> float:
    """Return the audited Tencent minute amount unit multiplier.

    Most Tencent minute feeds encode cumulative amount in yuan, but a subset
    of symbols has been observed with the amount scaled down by 100.  That
    made a perfectly ordinary 293-yuan stock appear to trade at a 2.93-yuan
    VWAP and could therefore create a false ``above_vwap`` confirmation.
    Infer only the exact, two-order-of-magnitude variant from the implied
    VWAP; do not attempt to "correct" arbitrary provider values.
    """
    if price <= 0 or cumulative_volume_lot <= 0 or cumulative_amount <= 0:
        return 1.0
    raw_vwap = cumulative_amount / (cumulative_volume_lot * 100)
    if raw_vwap <= 0:
        return 1.0
    ratio = price / raw_vwap
    return 100.0 if 80.0 <= ratio <= 120.0 else 1.0


async def tencent_intraday_minutes(symbol: str) -> list[dict[str, Any]]:
    """Return today's Tencent minute tape with non-look-ahead volume deltas.

    Tencent publishes cumulative volume in board lots and cumulative amount.
    Keeping both the raw cumulative values and the per-minute differences lets
    the signal layer compare a current burst with only earlier minutes.  The
    last row may be an in-progress minute and is labelled accordingly.
    """
    key = tencent_symbol(symbol)
    async with public_http_client() as client:
        response = await _request_with_retry(client, "GET", "https://web.ifzq.gtimg.cn/appstock/app/minute/query", params={"code": key}, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    payload = response.json()
    values = (((payload.get("data") or {}).get(key) or {}).get("data") or {}).get("data") or []
    if not isinstance(values, list):
        raise FreeProviderError("Tencent returned an invalid intraday minute payload")
    rows: list[dict[str, Any]] = []
    previous_volume = 0
    previous_amount = 0.0
    amount_scale: float | None = None
    segment = 0
    for value in values:
        parts = str(value).split()
        if len(parts) != 4 or not re.fullmatch(r"\d{4}", parts[0]):
            continue
        try:
            price, cumulative_volume, cumulative_amount = float(parts[1]), int(parts[2]), float(parts[3])
        except ValueError:
            continue
        if amount_scale is None:
            amount_scale = tencent_minute_amount_scale(
                price=price, cumulative_volume_lot=cumulative_volume,
                cumulative_amount=cumulative_amount,
            )
        normalized_amount = cumulative_amount * amount_scale
        # Tencent occasionally begins a new cumulative segment after the
        # midday break.  It is not valid to manufacture a negative minute;
        # reset the delta origin and label the boundary so downstream returns
        # and baselines never bridge the two segments.
        cumulative_reset = cumulative_volume < previous_volume or normalized_amount < previous_amount
        if cumulative_reset:
            segment += 1
            previous_volume, previous_amount = 0, 0.0
        rows.append({
            "ts_code": symbol, "time": parts[0], "close": price,
            "cumulative_volume_lot": cumulative_volume, "cumulative_amount": normalized_amount,
            "amount_unit_scale": amount_scale,
            "volume_lot": cumulative_volume - previous_volume, "amount": normalized_amount - previous_amount,
            "vwap": normalized_amount / (cumulative_volume * 100) if cumulative_volume else None,
            "cumulative_segment": segment, "cumulative_reset": cumulative_reset,
            "source": "tencent_free",
        })
        previous_volume, previous_amount = cumulative_volume, normalized_amount
    if rows:
        now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%H%M")
        for row in rows:
            row["is_complete"] = row["time"] < now
    return rows


def _tencent_order_book_row(symbol: str, values: list[str]) -> dict[str, Any] | None:
    """Decode Tencent's documented-in-practice single-quote depth layout.

    The all-A endpoint intentionally omits the order book.  The bounded
    single-quote layout has current/pre-open price, cumulative volume/amount,
    outer/inner volume, then five bid and five ask price/size pairs.  We keep
    the original field positions in ``raw_fields`` so any future upstream
    layout change is auditable rather than silently reinterpreted.
    """
    if len(values) < 29:
        return None
    try:
        price, pre_close = float(values[3]), float(values[4])
        cumulative_volume, outer_volume, inner_volume = float(values[6]), float(values[7]), float(values[8])
        # Field 35 is ``last/accumulated-volume/accumulated-amount`` in the
        # live single-quote payload.  The terminal component is not a pure
        # number when an upstream adds a suffix, so retain null rather than
        # fabricating interval VWAP.
        cumulative_amount = float(values[35].split("/")[-1]) if len(values) > 35 and "/" in values[35] and re.fullmatch(r"-?\d+(?:\.\d+)?", values[35].split("/")[-1]) else None
    except (TypeError, ValueError):
        return None
    # Preserve all five positions, including a zero/empty side at a limit-up
    # or limit-down.  A sealed limit book is the most important observation to
    # retain for later seal/erosion research, not an invalid quote to discard.
    bids: list[dict[str, float]] = []
    asks: list[dict[str, float]] = []
    try:
        for level in range(5):
            bid_offset, ask_offset = 9 + level * 2, 19 + level * 2
            bid_price, bid_size = float(values[bid_offset]), float(values[bid_offset + 1])
            ask_price, ask_size = float(values[ask_offset]), float(values[ask_offset + 1])
            bids.append({"price": bid_price, "size": bid_size})
            asks.append({"price": ask_price, "size": ask_size})
    except (TypeError, ValueError):
        return None
    valid_bids = [row for row in bids if row["price"] > 0 and row["size"] >= 0]
    valid_asks = [row for row in asks if row["price"] > 0 and row["size"] >= 0]
    if (not valid_bids and not valid_asks) or price <= 0 or pre_close <= 0:
        return None
    return {
        "ts_code": symbol, "name": values[1], "price": price, "pre_close": pre_close,
        "cumulative_volume_lot": cumulative_volume, "cumulative_amount": cumulative_amount,
        "outer_volume_lot": outer_volume, "inner_volume_lot": inner_volume,
        "bids": bids, "asks": asks,
        "one_sided_book": bool(valid_bids) != bool(valid_asks),
        "book_side": "bid_only" if valid_bids and not valid_asks else "ask_only" if valid_asks and not valid_bids else "two_sided",
        "seal_volume_lot": (valid_bids[0]["size"] if valid_bids and not valid_asks else
                            valid_asks[0]["size"] if valid_asks and not valid_bids else None),
        "trade_time": values[30] if len(values) > 30 else None,
        "raw_fields": values[:40], "source": "tencent_order_book",
    }


async def tencent_order_book_quotes(symbols: list[str], *, max_symbols: int = 20) -> list[dict[str, Any]]:
    """Fetch one bounded Tencent depth snapshot for explicit watchlist symbols.

    A single comma-separated request is used for the whole watchlist.  This is
    The caller supplies a bounded cap. Depth collection uses 20; the live
    watch quote path may use 40 in the same batched request. Neither is a
    general market-depth scraper.
    """
    normalized = list(dict.fromkeys(str(symbol).upper() for symbol in symbols if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", str(symbol).upper())))
    if not normalized:
        return []
    if max_symbols < 1 or max_symbols > 80:
        raise ValueError("Tencent quote batch cap must be between 1 and 80")
    if len(normalized) > max_symbols:
        raise ValueError(f"Tencent order-book observations are capped at {max_symbols} watchlist symbols")
    keys = [tencent_symbol(symbol) for symbol in normalized]
    by_key = {key.lower(): symbol for key, symbol in zip(keys, normalized, strict=True)}
    async with public_http_client() as client:
        response = await _request_with_retry(
            client, "GET", f"https://qt.gtimg.cn/q={','.join(keys)}",
            headers={"Referer": "https://gu.qq.com", "User-Agent": "Mozilla/5.0"}, timeout=8,
        )
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r'v_([a-z0-9]+)="([^"]*)";', response.text, re.I):
        symbol = by_key.get(match.group(1).lower())
        if not symbol:
            continue
        row = _tencent_order_book_row(symbol, match.group(2).split("~"))
        if row is not None:
            rows.append(row)
    return rows


async def sina_quotes(symbols: list[str], *, batch_size: int = 80, concurrency: int = 2) -> list[dict[str, Any]]:
    """Fetch bounded batches of public quotes for a supplemental market view.

    This is intentionally a low-concurrency, best-effort adapter.  Callers
    must mark its output as non-decision-eligible unless a licensed feed also
    meets the same freshness and coverage requirements.
    """
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    if concurrency < 1 or concurrency > 8:
        raise ValueError("concurrency must be between 1 and 8")
    chunks = [symbols[index:index + batch_size] for index in range(0, len(symbols), batch_size)]
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch_chunk(client: httpx.AsyncClient, chunk: list[str]) -> list[dict[str, Any]]:
        keys = [tencent_symbol(symbol) for symbol in chunk]
        mapping = {key.lower(): symbol for key, symbol in zip(keys, chunk, strict=True)}
        async with semaphore:
            response = await _request_with_retry(
                client, "GET", f"https://hq.sinajs.cn/list={','.join(keys)}",
                headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}, timeout=12,
            )
        return parse_sina_quote_batch(response.text, mapping)

    async with public_http_client() as client:
        batches = await asyncio.gather(*(fetch_chunk(client, chunk) for chunk in chunks), return_exceptions=True)
    rows: list[dict[str, Any]] = []
    errors = [item for item in batches if isinstance(item, Exception)]
    for item in batches:
        if isinstance(item, list):
            rows.extend(item)
    if errors and not rows:
        raise FreeProviderError("Sina batch quote requests failed") from errors[0]
    return rows
