"""Adapters for the configured external Tushare-compatible providers.

Provider credentials are intentionally read only from environment variables.
The adapters return a single normalized row shape while preserving the caller's
responsibility to retain raw source evidence.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping

import httpx
import requests

from requests.adapters import HTTPAdapter

from .capability_registry import provider_order
from .http_clients import provider_http_client
from .http_retry import retry_delay_seconds
from .runtime_executors import BlockingExecutorBoundary, bounded_queue_size
from .tushare_official import REALTIME_MARKET_HOURS_APIS


ProviderName = Literal["primary", "super_sdk", "super_get", "backup"]
ProviderPreference = Literal["auto", "primary", "super", "super_sdk", "super_get", "backup"]

# Only APIs that returned a structurally valid response through the dedicated
# GET + X-API-Key gateway belong here.  This is an observed routing allow-list,
# not a copy of the supplier's advertised catalog.
SUPER_GET_VERIFIED_APIS = frozenset({
    "daily", "daily_basic", "index_daily", "stock_basic", "fut_basic", "cn_gdp",
    "cyq_perf", "cyq_chips", "moneyflow", "moneyflow_ths", "moneyflow_dc",
    "moneyflow_ind_ths", "moneyflow_ind_dc", "moneyflow_mkt_dc", "moneyflow_hsgt",
    "moneyflow_cnt_ths", "stk_factor_pro", "report_rc", "top_list", "top_inst",
    "hm_list", "hm_detail", "limit_list_ths", "limit_list_d", "limit_step",
    "limit_cpt_list", "ths_hot", "dc_hot", "ths_index", "ths_daily", "ths_member",
    "kpl_list", "rt_k", "rt_min", "rt_min_daily", "rt_etf_k", "rt_idx_k",
    "rt_sw_k", "rt_fut_min", "rt_fut_min_daily",
})
SUPER_GET_REALTIME_APIS = frozenset({
    "rt_k", "rt_min", "rt_min_daily", "rt_etf_k", "rt_idx_k", "rt_sw_k",
    "rt_fut_min", "rt_fut_min_daily",
})
SUPER_SDK_REALTIME_APIS = frozenset({
    "rt_k", "rt_min", "rt_etf_k", "rt_etf_min", "rt_idx_k", "rt_idx_min", "rt_sw_k",
})
SUPER_GET_BOUNDED_ONLY_APIS = frozenset({"ths_member", "ths_index"})
# The GET reference snapshot is callable and unbounded, but the same-day
# primary stock_basic cross-section contained nine additional active symbols.
# Keep it as a fallback/cross-check rather than labeling it canonical-complete.
SUPER_GET_RECONCILIATION_APIS = frozenset({"stock_basic", "top_list", "top_inst"})
# The public ``super`` alias must never silently fall back to an SDK response
# for these routes.  Daily was explicitly migrated to GET; SDK realtime probes
# were observed stale or unsupported.
SUPER_GET_EXCLUSIVE_APIS = frozenset({"daily", "rt_min_daily", "rt_fut_min", "rt_fut_min_daily"})

# Per-interface routing is based on the 2026-08-11 live audit.  City wins when
# it is faster, timestamped, or demonstrably more complete; GET wins when City
# is empty, unavailable, or ignores the requested scope.
SUPER_REALTIME_PROVIDER_ORDER: dict[str, tuple[ProviderName, ...]] = {
    "rt_k": ("super_sdk", "super_get"),
    "rt_min": ("super_sdk", "super_get"),
    "rt_min_daily": ("super_get",),
    "rt_etf_k": ("super_sdk", "super_get"),
    "rt_etf_min": ("super_sdk",),
    "rt_idx_k": ("super_sdk", "super_get"),
    "rt_idx_min": ("super_sdk",),
    "rt_sw_k": ("super_get", "super_sdk"),
    "rt_fut_min": ("super_get",),
    "rt_fut_min_daily": ("super_get",),
}
SUPER_CITY_FIRST_APIS = frozenset({
    "cyq_perf", "cyq_chips", "moneyflow", "moneyflow_ths", "moneyflow_dc",
    "moneyflow_ind_ths", "moneyflow_mkt_dc", "moneyflow_hsgt", "moneyflow_cnt_ths",
    "report_rc", "top_list", "top_inst", "hm_detail", "limit_list_ths", "limit_list_d",
    "limit_step", "limit_cpt_list", "ths_hot", "ths_index", "ths_daily", "ths_member",
    "kpl_list",
})
class ProviderCallError(RuntimeError):
    """A token-free provider error that may trigger a fallback."""

    def __init__(self, message: str, failures: tuple[tuple[str, str], ...] = ()) -> None:
        super().__init__(message)
        self.failures = failures


@dataclass(frozen=True)
class TushareProvider:
    name: ProviderName
    key: str
    label: str
    endpoint: str
    credential: str
    protocol: Literal["standard", "sdk_path", "get_x_api_key", "backup_rest"]
    proxy_url: str = ""
    rate_limit_per_minute: int = 1
    fallback_credential: str = ""
    min_interval_seconds: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.credential)

    def supports(self, api_name: str) -> bool:
        if self.name == "backup":
            return api_name == "stock_basic"
        if self.name == "super_get":
            return api_name in SUPER_GET_VERIFIED_APIS
        if self.name == "super_sdk" and api_name in REALTIME_MARKET_HOURS_APIS:
            return api_name in SUPER_SDK_REALTIME_APIS
        if self.name == "primary" and api_name in REALTIME_MARKET_HOURS_APIS:
            return False
        return True

    def uses_super_get(self, api_name: str) -> bool:
        return self.protocol == "get_x_api_key" and api_name in SUPER_GET_VERIFIED_APIS


class ProviderRateLimiter:
    """Process-local rolling-window and minimum-spacing provider guard."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[str, deque[float]] = {}
        self._last_started_at: dict[str, float] = {}

    async def acquire(self, provider_key: str, limit_per_minute: int, min_interval_seconds: float = 0.0) -> None:
        limit = max(1, limit_per_minute)
        min_interval = max(0.0, min(60.0, min_interval_seconds))
        while True:
            now = time.monotonic()
            async with self._lock:
                requests = self._requests.setdefault(provider_key, deque())
                while requests and now - requests[0] >= 60:
                    requests.popleft()
                window_delay = max(0.0, 60 - (now - requests[0])) if len(requests) >= limit else 0.0
                spacing_delay = max(0.0, min_interval - (now - self._last_started_at.get(provider_key, 0.0)))
                delay = max(window_delay, spacing_delay)
                if delay <= 0:
                    requests.append(now)
                    self._last_started_at[provider_key] = now
                    return
            await asyncio.sleep(max(0.01, delay))


request_limiter = ProviderRateLimiter()
_super_get_thread_state = threading.local()
ProviderRequestReserver = Callable[[str, int, float], Awaitable[None]]
_provider_request_reserver: ProviderRequestReserver | None = None
_provider_request_reserver_max_wait_seconds: float | None = None


def configure_provider_request_reserver(reserver: ProviderRequestReserver | None, *,
                                        max_wait_seconds: float | None = None) -> None:
    """Attach the lifespan-owned cross-process pacing callback, if available."""
    global _provider_request_reserver, _provider_request_reserver_max_wait_seconds
    _provider_request_reserver = reserver
    _provider_request_reserver_max_wait_seconds = None if reserver is None else max_wait_seconds


def provider_request_reservation_status() -> dict[str, bool | float | None]:
    """Return local coordination configuration without probing a provider."""
    return {
        "process_local_limiter": True,
        "shared_database_reservation": _provider_request_reserver is not None,
        "shared_max_wait_seconds": _provider_request_reserver_max_wait_seconds,
    }


async def acquire_provider_request_slot(provider: TushareProvider) -> None:
    """Apply shared reservation first, then the existing in-process limiter."""
    reserver = _provider_request_reserver
    if reserver is not None:
        await reserver(provider.key, provider.rate_limit_per_minute, provider.min_interval_seconds)
    await request_limiter.acquire(provider.key, provider.rate_limit_per_minute, provider.min_interval_seconds)


def _super_get_worker_count() -> int:
    try:
        return min(16, max(1, int(os.getenv("TUSHARE_SUPER_GET_MAX_WORKERS", "8"))))
    except ValueError:
        return 8


# Slow proxy DNS/TLS work must never consume asyncio's shared default executor,
# which also runs AKShare calls used by the 30-second monitoring loop.
_super_get_executor_workers = _super_get_worker_count()
_super_get_executor = ThreadPoolExecutor(max_workers=_super_get_executor_workers, thread_name_prefix="super-get")
_super_get_queue_capacity = bounded_queue_size("TUSHARE_SUPER_GET_MAX_QUEUE", 8)
_super_get_executor_boundary = BlockingExecutorBoundary(
    "super_get", _super_get_executor_workers, _super_get_queue_capacity,
)


def super_get_executor_status() -> dict[str, int]:
    """Return local proxy-worker capacity without probing the gateway."""
    return _super_get_executor_boundary.status()


def shutdown_super_get_executor() -> None:
    """Stop accepting queued proxy work during service shutdown."""
    _super_get_executor.shutdown(wait=False, cancel_futures=True)


def safe_error_detail(value: str, limit: int = 500) -> str:
    """Keep supplier diagnostics without retaining credentials in evidence."""
    compact = value.strip().replace("\n", " ")
    # Authorization values conventionally contain a scheme plus a whitespace
    # separated credential (for example ``Bearer token``).  Handle the whole
    # header before generic key/value redaction so the credential cannot remain
    # after the scheme is replaced.
    compact = re.sub(r"(?i)\bauthorization\b\s*[:=]\s*[^,;&]+", "Authorization: <redacted>", compact)
    compact = re.sub(
        r"(?i)\b(x-api-key|authorization|api[_-]?key|access[_-]?token|token)\b\s*([:=])\s*([^\s,&;]+)",
        r"\1\2<redacted>",
        compact,
    )
    # Some gateways echo bearer headers as a separated phrase rather than a
    # key/value pair.
    compact = re.sub(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+", "Bearer <redacted>", compact)
    return compact[:limit]


async def provider_http_request(provider: TushareProvider, operation: Any) -> httpx.Response:
    """Apply bounded retry only to transient HTTP/provider transport errors."""
    transient = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(2):
        await acquire_provider_request_slot(provider)
        response_headers: Any | None = None
        try:
            response = await operation()
            response_headers = response.headers
        except httpx.HTTPError as error:
            last_error = error
            if attempt == 1:
                raise
        else:
            if response.status_code not in transient or attempt == 1:
                return response
        await asyncio.sleep(retry_delay_seconds(response_headers, 0.8 * (attempt + 1)))
    raise last_error or ProviderCallError("provider request failed without response")


def _super_get_session(proxy_url: str) -> requests.Session:
    """Reuse one proxy/TLS connection pool per worker thread.

    ``requests.Session`` is not documented as thread-safe, while Super GET is
    dispatched with the bounded Super GET executor. Thread-local sessions preserve
    connection reuse without sharing mutable state across concurrent workers.
    """
    sessions = getattr(_super_get_thread_state, "sessions", None)
    if sessions is None:
        sessions = {}
        _super_get_thread_state.sessions = sessions
    session = sessions.get(proxy_url)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        if proxy_url:
            session.proxies.update({"http": proxy_url, "https": proxy_url})
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0, pool_block=True)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        sessions[proxy_url] = session
    return session


def _super_get_http_get(url: str, *, params: dict[str, Any], credential: str,
                        proxy_url: str, timeout: float) -> requests.Response:
    return _super_get_session(proxy_url).get(
        url, params=params, headers={"X-API-Key": credential}, timeout=timeout,
    )


def bounded_rate_limit(value: str | None, default: int) -> int:
    try:
        return min(600, max(1, int(value or default)))
    except ValueError:
        return default


def bounded_interval(value: str | None, default: float) -> float:
    try:
        return min(60.0, max(0.0, float(value if value not in (None, "") else default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ProviderCall:
    provider: TushareProvider
    rows: list[dict[str, Any]]
    failed_providers: tuple[tuple[str, str], ...] = ()
    empty_providers: tuple[str, ...] = ()
    complete: bool = True
    pages: int = 1


def provider_configs(environ: Mapping[str, str] | None = None) -> dict[ProviderName, TushareProvider]:
    """Build configuration with backwards-compatible primary env names."""
    env = os.environ if environ is None else environ
    primary_token = (env.get("TUSHARE_PRIMARY_TOKEN") or env.get("TUSHARE_TOKEN") or "").strip()
    primary_url = (env.get("TUSHARE_PRIMARY_API_URL") or env.get("TUSHARE_API_URL") or "").strip().rstrip("/")
    super_token = (env.get("TUSHARE_SUPER_SDK_TOKEN") or env.get("TUSHARE_SUPER_TOKEN") or "").strip()
    super_url = (env.get("TUSHARE_SUPER_SDK_API_URL") or env.get("TUSHARE_SUPER_API_URL") or "").strip().rstrip("/")
    super_proxy_url = (env.get("TUSHARE_SUPER_SDK_PROXY_URL") or env.get("TUSHARE_SUPER_PROXY_URL") or "").strip()
    super_realtime_key = (env.get("TUSHARE_SUPER_GET_API_KEY") or env.get("TUSHARE_SUPER_REALTIME_API_KEY") or "").strip()
    super_realtime_fallback_key = (env.get("TUSHARE_SUPER_GET_FALLBACK_API_KEY") or env.get("TUSHARE_SUPER_REALTIME_FALLBACK_API_KEY") or "").strip()
    super_realtime_url = (env.get("TUSHARE_SUPER_GET_API_URL") or env.get("TUSHARE_SUPER_REALTIME_API_URL") or "").strip().rstrip("/")
    super_realtime_proxy_url = (env.get("TUSHARE_SUPER_GET_PROXY_URL") or env.get("TUSHARE_SUPER_REALTIME_PROXY_URL") or "").strip()
    backup_key = (env.get("TUSHARE_BACKUP_API_KEY") or "").strip()
    backup_url = (env.get("TUSHARE_BACKUP_API_URL") or "").strip().rstrip("/")
    return {
        "primary": TushareProvider("primary", "tushare_primary", "Tushare 兼容主源", primary_url, primary_token, "standard", "", bounded_rate_limit(env.get("TUSHARE_PRIMARY_REQUESTS_PER_MINUTE"), 60)),
        "super_sdk": TushareProvider(
            "super_sdk", "tushare_super_sdk", "Tushare 超级 SDK 代理源", super_url, super_token, "sdk_path",
            super_proxy_url, bounded_rate_limit(env.get("TUSHARE_SUPER_SDK_REQUESTS_PER_MINUTE") or env.get("TUSHARE_SUPER_REQUESTS_PER_MINUTE"), 30),
        ),
        "super_get": TushareProvider(
            "super_get", "tushare_super_get", "Tushare 超级 GET 网关", super_realtime_url, super_realtime_key,
            "get_x_api_key", super_realtime_proxy_url,
            bounded_rate_limit(env.get("TUSHARE_SUPER_GET_REQUESTS_PER_MINUTE") or env.get("TUSHARE_SUPER_REALTIME_REQUESTS_PER_MINUTE"), 60),
            super_realtime_fallback_key, bounded_interval(env.get("TUSHARE_SUPER_GET_MIN_INTERVAL_SECONDS"), 1.0),
        ),
        "backup": TushareProvider("backup", "tushare_backup", "Tushare REST 备用源", backup_url, backup_key, "backup_rest", "", bounded_rate_limit(env.get("TUSHARE_BACKUP_REQUESTS_PER_MINUTE"), 6)),
    }


def _expand_provider_name(api_name: str, name: str) -> tuple[ProviderName, ...]:
    if name != "super":
        return (name,)  # type: ignore[return-value]
    if api_name in SUPER_REALTIME_PROVIDER_ORDER:
        return SUPER_REALTIME_PROVIDER_ORDER[api_name]
    if api_name in SUPER_GET_EXCLUSIVE_APIS:
        return ("super_get",)
    if api_name in SUPER_CITY_FIRST_APIS:
        # City is complete for broad board/member and richer event routes;
        # the GET route remains a bounded or availability fallback.
        return ("super_sdk", "super_get")
    if api_name in SUPER_GET_VERIFIED_APIS:
        return ("super_get", "super_sdk")
    return ("super_sdk",)


def provider_candidates(api_name: str, preferred: ProviderPreference = "auto", *, environ: Mapping[str, str] | None = None) -> list[TushareProvider]:
    configs = provider_configs(environ)
    requested_names = provider_order(api_name) if preferred == "auto" else (preferred,)
    names: list[ProviderName] = []
    for requested_name in requested_names:
        for name in _expand_provider_name(api_name, requested_name):
            if name not in names:
                names.append(name)
    return [provider for name in names if (provider := configs[name]).configured and provider.supports(api_name)]


def provider_status(*, environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    def realtime_summary(provider: TushareProvider) -> tuple[str, str, list[str]]:
        if provider.name == "primary":
            # Live-session audit on 2026-08-10 established that this configured
            # route has no purchased/verified realtime family.  Keep it out of
            # the intraday decision path instead of inferring capability from a
            # configured token alone.
            return ("unavailable", "No verified realtime capability; live-family requests were unpurchased or rate-limited.", [])
        if provider.name == "super_get" and provider.configured:
            return ("verified_partial", "Verified GET realtime: stock, ETF/index/SW snapshots and stock/futures minute subsets; unsupported live families remain excluded.", sorted(SUPER_GET_REALTIME_APIS))
        if provider.name == "super_sdk" and provider.configured:
            return ("verified_partial", "Verified City realtime: timestamped stock/ETF/index snapshots and single-minute routes plus SW snapshots; *_min_daily routes remain unavailable.", sorted(SUPER_SDK_REALTIME_APIS))
        return ("not_applicable", "No realtime route is configured for this provider.", [])

    entries: list[dict[str, Any]] = []
    sdk_first_apis = SUPER_CITY_FIRST_APIS | frozenset(
        api_name for api_name, order in SUPER_REALTIME_PROVIDER_ORDER.items() if order[0] == "super_sdk"
    )
    get_first_apis = (
        SUPER_GET_VERIFIED_APIS - SUPER_CITY_FIRST_APIS - frozenset(SUPER_REALTIME_PROVIDER_ORDER)
    ) | frozenset(
        api_name for api_name, order in SUPER_REALTIME_PROVIDER_ORDER.items() if order[0] == "super_get"
    )
    for provider in provider_configs(environ).values():
        realtime_coverage, realtime_note, verified_get_apis = realtime_summary(provider)
        entries.append({
            "name": provider.name, "provider_key": provider.key, "label": provider.label, "configured": provider.configured,
            "protocol": provider.protocol,
            "realtime_protocol": "get_x_api_key" if provider.name == "super_get" else "sdk_post" if provider.name == "super_sdk" else "none",
            "realtime_configured": provider.name in {"super_get", "super_sdk"} and provider.configured,
            "realtime_coverage": realtime_coverage,
            "realtime_note": realtime_note,
            "realtime_apis": verified_get_apis,
            "super_alias_first_apis": sorted(sdk_first_apis) if provider.name == "super_sdk"
                                      else sorted(get_first_apis) if provider.name == "super_get" else [],
            "get_apis": sorted(SUPER_GET_VERIFIED_APIS) if provider.name == "super_get" else verified_get_apis,
            "complete_query_apis": sorted(SUPER_GET_VERIFIED_APIS - SUPER_GET_BOUNDED_ONLY_APIS - SUPER_GET_RECONCILIATION_APIS) if provider.name == "super_get" else [],
            "bounded_only_apis": sorted(SUPER_GET_BOUNDED_ONLY_APIS) if provider.name == "super_get" else [],
            "reconciliation_required_apis": sorted(SUPER_GET_RECONCILIATION_APIS) if provider.name == "super_get" else [],
            "rate_limit_per_minute": provider.rate_limit_per_minute,
            "min_interval_seconds": provider.min_interval_seconds,
        })
    return entries


def _decode_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    if body.get("code", 0) != 0:
        raise ProviderCallError(str(body.get("msg") or "provider returned an error"))
    data = body.get("data") or {}
    if isinstance(data, list):
        if not all(isinstance(item, dict) for item in data):
            raise ProviderCallError("provider realtime response has an invalid data shape")
        return [dict(item) for item in data]
    if not isinstance(data, dict):
        raise ProviderCallError("provider response has an invalid data shape")
    fields, items = data.get("fields") or [], data.get("items") or []
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ProviderCallError("provider response has an invalid data shape")
    if all(isinstance(item, dict) for item in items):
        # Some SDK-compatible gateways already expand rows into objects rather
        # than Tushare's usual positional lists.  Preserve those values as-is;
        # zipping a dict would otherwise turn field names into fake data.
        return [dict(item) for item in items]
    try:
        return [dict(zip(fields, item, strict=True)) for item in items]
    except ValueError as error:
        raise ProviderCallError("provider response fields do not match items") from error


def _filter_requested_realtime_rows(api_name: str, params: dict[str, Any],
                                    rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair live routes that return a full cross-section for one symbol."""
    requested = str(params.get("ts_code") or params.get("symbol") or "").strip().upper()
    if api_name not in REALTIME_MARKET_HOURS_APIS or not requested or not rows:
        return rows
    matched = [row for row in rows if str(row.get("ts_code") or row.get("code") or "").strip().upper() == requested]
    return matched


async def call_provider(provider: TushareProvider, api_name: str, params: dict[str, Any], fields: str | None) -> list[dict[str, Any]]:
    if not provider.configured:
        raise ProviderCallError(f"{provider.key} is not configured")
    if not provider.supports(api_name):
        raise ProviderCallError(f"{provider.key} does not support {api_name}")
    if provider.uses_super_get(api_name):
        credentials = [provider.credential]
        if provider.fallback_credential and provider.fallback_credential != provider.credential:
            credentials.append(provider.fallback_credential)
        realtime_request = api_name in SUPER_GET_REALTIME_APIS
        # A stale realtime request is less useful than a skipped sample. Try
        # each configured credential once with a short timeout, while keeping
        # the more tolerant retry contract for daily/reference queries.
        attempts_per_credential = 1 if realtime_request else 2
        request_timeout = 8 if realtime_request else 15
        failures: list[str] = []
        for credential in credentials:
            for attempt in range(attempts_per_credential):
                await acquire_provider_request_slot(provider)
                response_headers: Any | None = None
                try:
                    # The verified gateway/proxy pair requires requests-style
                    # proxy routing; httpx CONNECT receives 407 here. Reuse a
                    # thread-local Session so frequent realtime calls do not
                    # renegotiate the proxy/TLS connection on every request.
                    def proxy_http_get() -> requests.Response:
                        return _super_get_http_get(
                            f"{provider.endpoint}/{api_name}", params=params,
                            credential=credential, proxy_url=provider.proxy_url, timeout=request_timeout,
                        )

                    response = await _super_get_executor_boundary.run(
                        _super_get_executor, proxy_http_get, timeout_seconds=request_timeout + 2,
                    )
                    response_headers = response.headers
                    if response.ok:
                        return _filter_requested_realtime_rows(api_name, params, _decode_rows(response.json()))
                    detail = safe_error_detail(response.text, 180)
                    failures.append(f"HTTP {response.status_code}: {detail or response.reason}")
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        break
                except (requests.RequestException, ValueError) as error:
                    failures.append(type(error).__name__)
                if attempt + 1 < attempts_per_credential:
                    await asyncio.sleep(retry_delay_seconds(response_headers, 0.8))
        raise ProviderCallError("super GET gateway failed with configured credentials: " + "; ".join(failures))

    async with provider_http_client(provider.key, provider.proxy_url) as client:
        if provider.protocol == "backup_rest":
            response = await provider_http_request(provider, lambda: client.get(
                f"{provider.endpoint}/app-api/openapi/v1/tushare/stock-basic",
                headers={"X-API-Key": provider.credential}, params={**params, "limit": min(int(params.get("limit", 100)), 100)},
            ))
            response.raise_for_status()
            body = response.json()
            if body.get("code", 0) != 0:
                raise ProviderCallError(str(body.get("msg") or "backup provider returned an error"))
            data = body.get("data") or {}
            items = data.get("items") or []
            if not isinstance(items, list):
                raise ProviderCallError("backup provider response has an invalid data shape")
            if all(isinstance(item, dict) for item in items):
                return [dict(item) for item in items]
            return _decode_rows({"code": 0, "data": data})

        provider_params = dict(params)
        if provider.protocol == "sdk_path":
            # Match DataApi.query(): its gateway requires both the API path
            # and this SDK-internal marker inside the parameter payload.
            provider_params.setdefault("ts_type_name", provider.endpoint)
        payload: dict[str, Any] = {"api_name": api_name, "token": provider.credential, "params": provider_params}
        if fields or provider.protocol == "sdk_path":
            payload["fields"] = fields or ""
        endpoint = f"{provider.endpoint}/{api_name}" if provider.protocol == "sdk_path" else provider.endpoint
        response = await provider_http_request(provider, lambda: client.post(endpoint, json=payload))
        if response.is_error:
            # Gateway 400/404 bodies often distinguish a bad parameter from an
            # unimplemented SDK path. Preserve that evidence for the catalog.
            detail = safe_error_detail(response.text)
            raise ProviderCallError(f"HTTP {response.status_code}: {detail or response.reason_phrase}")
    return _filter_requested_realtime_rows(api_name, params, _decode_rows(response.json()))


def _normalize_ths_member_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Repair the audited SDK field-layout duplicate and keep one row/member.

    The SDK gateway currently concatenates two layouts for broad THS boards:
    one places the constituent name in ``is_new`` and one in ``con_name``.
    Official ``is_new`` values are only Y/N, so any other text is safely moved
    to ``con_name`` before exact ``con_code`` deduplication.
    """
    members: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        code = str(row.get("con_code") or "").strip().upper()
        if not code:
            continue
        is_new = str(row.get("is_new") or "").strip()
        if is_new and is_new not in {"Y", "N"}:
            row["con_name"] = row.get("con_name") or is_new
            row["is_new"] = None
        row["con_code"] = code
        existing = members.get(code)
        if existing is None:
            members[code] = row
            continue
        for key, value in row.items():
            if existing.get(key) in (None, "") and value not in (None, ""):
                existing[key] = value
    return [members[code] for code in sorted(members)]


async def call_with_fallback(api_name: str, params: dict[str, Any], fields: str | None,
                             preferred: ProviderPreference = "auto", *, paginate: bool = False,
                             page_size: int = 1000, max_rows: int = 10_000,
                             max_pages: int = 20, require_complete: bool = False,
                             blocked_provider_keys: set[str] | None = None) -> ProviderCall:
    """Call one provider, optionally proving completeness with offset paging.

    A paginated result is complete only after the same provider returns a page
    shorter than ``page_size``.  If a page fails, all pages from that provider
    are discarded before fallback, so rows from distinct contracts are never
    combined into a synthetic snapshot.
    """
    candidates = [provider for provider in provider_candidates(api_name, preferred)
                  if provider.key not in (blocked_provider_keys or set())]
    if not candidates:
        raise ProviderCallError(f"no configured provider supports {api_name} for preference {preferred}")
    failures: list[tuple[str, str]] = []
    empty_providers: list[str] = []
    first_empty_provider: TushareProvider | None = None
    # Empty is valid source evidence but not proof that every compatible source
    # is empty. Automatic/alias routing tries the next audited contract and
    # returns the first empty response only when no candidate has real rows.
    # An explicitly selected physical provider remains a strict one-source call.
    fallback_on_empty = preferred in {"auto", "super"} and len(candidates) > 1
    for provider in candidates:
        try:
            if not paginate:
                rows = await call_provider(provider, api_name, params, fields)
                if api_name == "ths_member":
                    rows = _normalize_ths_member_rows(rows)
                if not rows and fallback_on_empty:
                    first_empty_provider = first_empty_provider or provider
                    empty_providers.append(provider.key)
                    continue
                return ProviderCall(provider=provider, rows=rows, failed_providers=tuple(failures),
                                    empty_providers=tuple(empty_providers))

            if api_name == "ths_member" and provider.name == "super_get" and require_complete:
                raise ProviderCallError(
                    "tushare_super_get caps broad ths_member boards and ignores offset; "
                    "use provider=super or super_sdk for a complete single-board response"
                )
            if api_name == "ths_member" and provider.name == "super_sdk":
                rows = _normalize_ths_member_rows(await call_provider(provider, api_name, params, fields))
                if not rows and fallback_on_empty:
                    first_empty_provider = first_empty_provider or provider
                    empty_providers.append(provider.key)
                    continue
                if len(rows) > max_rows:
                    raise ProviderCallError(
                        f"{provider.key} returned {len(rows)} unique ths_member rows, above the {max_rows}-row safety bound"
                    )
                return ProviderCall(
                    provider=provider, rows=rows, failed_providers=tuple(failures),
                    empty_providers=tuple(empty_providers), complete=True, pages=1,
                )

            rows: list[dict[str, Any]] = []
            seen_pages: set[str] = set()
            base_offset = max(0, int(params.get("offset", 0) or 0))
            bounded_page_size = min(3000, max(1, page_size))
            complete = False
            pages = 0
            while pages < max(1, max_pages):
                page_params = {**params, "limit": bounded_page_size, "offset": base_offset + len(rows)}
                page = await call_provider(provider, api_name, page_params, fields)
                pages += 1
                page_fingerprint = str(hash(tuple(
                    tuple(sorted((str(key), str(value)) for key, value in row.items()))
                    for row in page
                )))
                if page and page_fingerprint in seen_pages:
                    raise ProviderCallError(f"{provider.key} ignored pagination offset for {api_name}")
                seen_pages.add(page_fingerprint)
                rows.extend(page)
                if len(page) < bounded_page_size:
                    complete = True
                    break
                if len(rows) >= max_rows:
                    break
            if require_complete and not complete:
                raise ProviderCallError(
                    f"{provider.key} did not reach a terminal page for {api_name} "
                    f"within {max_pages} pages/{max_rows} rows"
                )
            if not rows and fallback_on_empty:
                first_empty_provider = first_empty_provider or provider
                empty_providers.append(provider.key)
                continue
            return ProviderCall(
                provider=provider, rows=rows, failed_providers=tuple(failures),
                empty_providers=tuple(empty_providers), complete=complete, pages=pages,
            )
        except (httpx.HTTPError, ProviderCallError, ValueError) as error:
            detail = safe_error_detail(str(error)) or type(error).__name__
            failures.append((provider.key, detail[:300]))
    if first_empty_provider is not None:
        return ProviderCall(provider=first_empty_provider, rows=[], failed_providers=tuple(failures),
                            empty_providers=tuple(empty_providers))
    raise ProviderCallError(" | ".join(f"{key}: {error}" for key, error in failures), tuple(failures))
