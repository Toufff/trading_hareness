"""Small, token-free public-market adapters used as corroborating sources.

These endpoints are intentionally limited to a single symbol and short daily
window.  They are not a historical backfill channel and are kept separate from
the licensed Tushare-compatible providers.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any

import httpx

from .daily_bar_repository import yuan_to_thousand_yuan
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


def exchange_prefixed_code(symbol: str) -> str:
    """``600664.SH`` -> ``sh600664``: the key format Sina's quote list expects."""
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
        # Eastmoney's kline volume (f56) is already in lots, matching the
        # canonical convention, but its amount (f57) is in yuan; convert it
        # to thousand yuan here so the guard in daily_bar_repository does not
        # have to special-case this provider.
        try:
            amount = yuan_to_thousand_yuan(Decimal(values[6])) if values[6] not in (None, "", "-") else None
        except InvalidOperation:
            amount = None
        rows.append({"ts_code": symbol, "trade_date": values[0].replace("-", ""), "open": values[1], "close": values[2],
                     "high": values[3], "low": values[4], "vol": values[5], "amount": str(amount) if amount is not None else None,
                     "pct_chg": values[8] if len(values) > 8 else None})
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
    names when the all-A cross-section misses its scan budget.
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


async def sina_quote(symbol: str) -> dict[str, Any] | None:
    key = exchange_prefixed_code(symbol)
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
        keys = [exchange_prefixed_code(symbol) for symbol in chunk]
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
