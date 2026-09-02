"""Adapters for the configured external Tushare-compatible providers.

Provider credentials are intentionally read only from environment variables.
The adapters return a single normalized row shape while preserving the caller's
responsibility to retain raw source evidence.
"""

from __future__ import annotations

import asyncio
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
from .network_health import network_state
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
# ProMax uses the same GET + X-API-Key wire contract but is an independent
# gateway.  Do not inherit the legacy gateway's broad allow-list merely because
# the URL shape matches: every entry below was probed against ProMax and
# returned code=0 with real rows.
#
# The previous list held six APIs from a 2026-08-17 single-attempt probe.  That
# probe systematically under-reported: ProMax answers roughly 20% of single
# calls with a transient 503/504, so an API that succeeds on retry was recorded
# as unsupported.  Re-probed on 2026-08-26 with bounded retries, 43 of 45
# candidates returned data - including the whole moneyflow family, the limit
# pools, the THS/DC hot lists and the reporting calendar, none of which could
# route here before.
PROMAX_VERIFIED_APIS = frozenset({
    # daily bars, controls and reference
    "daily", "daily_basic", "stk_limit", "adj_factor", "suspend_d", "trade_cal",
    "stock_basic", "index_daily",
    # capital flow (all end-of-day; ProMax exposes no intraday flow route)
    "moneyflow", "moneyflow_dc", "moneyflow_ths", "moneyflow_ind_ths",
    "moneyflow_ind_dc", "moneyflow_mkt_dc", "moneyflow_hsgt", "moneyflow_cnt_ths",
    # chip distribution and factors
    "cyq_perf", "cyq_chips", "stk_factor_pro",
    # limit pools and sentiment
    "limit_list_d", "limit_list_ths", "limit_step", "limit_cpt_list", "kpl_list",
    "ths_hot", "dc_hot",
    # THS sector catalogue
    "ths_index", "ths_daily", "ths_member",
    # dragon-tiger and hot-money
    "top_list", "top_inst", "hm_list", "hm_detail", "report_rc",
    # reporting calendar and guidance
    "disclosure_date", "forecast", "express",
    # realtime
    "rt_k", "rt_min", "rt_min_daily", "rt_etf_k", "rt_idx_k", "rt_sw_k", "rt_fut_min",
    # historical minute bars - the only such source available anywhere in this
    # deployment.  Verified back to 2024-08 at 1-minute resolution.
    "stk_mins",
})
# ``rt_fut_min_daily`` is deliberately absent: it answered HTTP 503 on every one
# of four attempts, unlike every other route here.
#
# ProMax ``rt_k`` *is* a live quote - it carries a second-resolution
# ``updated_at`` plus level-1 bid/ask - which is why it is realtime here.  The
# separate SUPER_SDK_DELAYED_CONTEXT_APIS entry below describes the City SDK's
# rt_k, a different upstream that was observed unchanged across intraday
# samples; the two must not be conflated.
PROMAX_REALTIME_APIS = frozenset({
    "rt_k", "rt_min", "rt_min_daily", "rt_etf_k", "rt_idx_k", "rt_sw_k", "rt_fut_min",
})
# ``stk_factor_pro`` rejects a full-market ``trade_date`` cross-section on this
# gateway (HTTP 400) but serves a per-symbol range, so it must only be routed
# here when the caller has already bounded the request to a symbol.
# ``stk_mins`` serves one ts_code per request (a comma-separated batch returns
# nothing), so it must only be routed here for an already symbol-bounded call.
PROMAX_BOUNDED_ONLY_APIS = frozenset({"stk_factor_pro", "ths_member", "ths_index", "stk_mins"})
# City ``rt_k`` supplies only a trading date and was observed unchanged across
# repeated intraday samples on 2026-08-13.  It is retained as delayed
# cumulative quote context for research, but excluded from this verified
# realtime set so it cannot become a confirmation/trigger source by routing.
SUPER_SDK_REALTIME_APIS = frozenset({
    "rt_min", "rt_etf_min", "rt_idx_min", "rt_sw_k",
})
SUPER_SDK_DELAYED_CONTEXT_APIS = frozenset({"rt_k", "rt_etf_k", "rt_idx_k"})
SUPER_GET_BOUNDED_ONLY_APIS = frozenset({"ths_member", "ths_index"})
# The GET reference snapshot is callable and unbounded, but the same-day
# primary stock_basic cross-section contained nine additional active symbols.
# Keep it as a fallback/cross-check rather than labeling it canonical-complete.
SUPER_GET_RECONCILIATION_APIS = frozenset({"stock_basic", "top_list", "top_inst"})
# The public ``super`` alias must never silently fall back to an SDK response
# for these routes.  Daily was explicitly migrated to GET; SDK realtime probes
# were observed stale or unsupported.
SUPER_GET_EXCLUSIVE_APIS = frozenset({"daily", "rt_min_daily", "rt_fut_min", "rt_fut_min_daily"})

# DataHub's REST endpoint is an independently probed, read-only fallback.
# Keep this list deliberately smaller than the advertised Tushare catalog:
# every entry was observed with a valid ``code=0`` envelope on 2026-08-26,
# while realtime and historical-minute routes remain excluded from the
# fallback until their timestamp/retention contracts are separately verified.
DATAHUB_VERIFIED_APIS = frozenset({
    "stock_basic", "daily", "daily_basic", "adj_factor", "trade_cal", "index_daily",
    "stk_limit", "suspend_d", "weekly",
    "moneyflow", "moneyflow_dc", "moneyflow_ths", "moneyflow_ind_dc",
    "moneyflow_ind_ths", "moneyflow_mkt_dc", "moneyflow_cnt_ths", "moneyflow_hsgt",
    "limit_list_d", "limit_list_ths", "limit_step", "limit_cpt_list",
    "ths_index", "ths_daily", "ths_member", "ths_hot", "dc_hot", "kpl_list",
    "cyq_perf", "cyq_chips", "stk_factor_pro", "top_list", "top_inst", "report_rc",
    "forecast", "express", "income", "cashflow", "fina_mainbz", "fina_mainbz_vip",
    "disclosure_date", "dividend", "namechange", "new_share", "share_float",
    "stk_holdernumber", "stock_st", "etf_basic", "etf_share_size", "etf_sz_cons",
    "fund_daily", "fund_nav", "fund_share", "fut_basic", "fut_daily", "fut_settle",
    "fut_trade_cal", "fut_wsr", "ggt_daily", "ggt_top10", "hibor", "shibor",
    "shibor_quote", "sge_daily", "us_tbr", "us_tltr", "us_trltr", "us_trycr",
    "us_tycr", "cn_cpi", "cn_m", "cn_pmi", "cn_ppi", "eco_cal",
})

# Per-interface routing is based on the 2026-08-11 live audit.  City wins when
# it is faster, timestamped, or demonstrably more complete; GET wins when City
# is empty, unavailable, or ignores the requested scope.
SUPER_REALTIME_PROVIDER_ORDER: dict[str, tuple[ProviderName, ...]] = {
    # City rt_k is excluded by ``supports`` above.  GET remains an optional
    # context feed only; high-frequency strategy confirmation uses Tencent
    # plus timestamped rt_min, never an un-stamped rt_k quote.
    "rt_k": ("super_get",),
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


class ProviderRateLimitedError(ProviderCallError):
    """A provider explicitly asked the caller to slow down (HTTP 429 or similar).

    Distinct from a transport or contract failure: correct backpressure
    behaviour must not accumulate the same consecutive-failure state used to
    trip the generic circuit breaker (see ``provider_health.record_provider_failure``).
    """

    def __init__(self, message: str, failures: tuple[tuple[str, str], ...] = (),
                *, retry_after: float | None = None) -> None:
        super().__init__(message, failures)
        self.retry_after = retry_after


class ProviderUnauthorizedError(ProviderCallError):
    """The configured credential itself was rejected (HTTP 401/403/invalid token).

    A bad key fails every subsequent call identically; it is not evidence of
    upstream flakiness, so it is reported to ``provider_health`` as a distinct
    ``unauthorized`` status rather than folded into the generic circuit.
    """


#: Coarse text markers used to classify an already-formatted failure message
#: when the underlying HTTP status is not otherwise available to the caller
#: (the retry/fallback loops in this module join multiple attempts' messages
#: before raising). Matches are intentionally narrow: audit item 9 asks only
#: to separate genuine rate limiting/bad-credential responses, not every
#: "not purchased"/"not supported" capability rejection already handled by
#: ``provider_health.provider_error_availability``.
_RATE_LIMIT_MARKERS = ("http 429", "too many requests", "rate limit", "限流", "请求过于频繁")
_UNAUTHORIZED_MARKERS = (
    "http 401", "http 403", "unauthorized", "invalid token", "token expired",
    "invalid api key", "invalid credential", "token无效", "认证失败",
)


def classify_provider_error_text(message: str) -> tuple[type[ProviderCallError], float | None]:
    """Infer a coarse provider failure class from an already-formatted message.

    Returns the exception subclass to raise (or ``ProviderCallError`` itself
    when nothing matches) plus a ``Retry-After``-style hint in seconds when
    one could be parsed out of the text.
    """
    lowered = message.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        match = re.search(r"retry[-_ ]after\D{0,5}(\d+(?:\.\d+)?)", lowered)
        return ProviderRateLimitedError, (float(match.group(1)) if match else None)
    if any(marker in lowered for marker in _UNAUTHORIZED_MARKERS):
        return ProviderUnauthorizedError, None
    return ProviderCallError, None


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
    get_gateway_mode: Literal["legacy", "promax"] = "legacy"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.credential)

    def supports(self, api_name: str) -> bool:
        if self.name == "backup":
            return api_name in DATAHUB_VERIFIED_APIS
        if self.name == "super_get":
            return api_name in self.get_verified_apis
        if self.name == "super_sdk" and api_name in REALTIME_MARKET_HOURS_APIS:
            return api_name in SUPER_SDK_REALTIME_APIS or api_name in SUPER_SDK_DELAYED_CONTEXT_APIS
        if self.name == "primary" and api_name in REALTIME_MARKET_HOURS_APIS:
            return False
        return True

    def uses_super_get(self, api_name: str) -> bool:
        return self.protocol == "get_x_api_key" and api_name in self.get_verified_apis

    def requires_bounded_request(self, api_name: str) -> bool:
        """True when this gateway serves the API only for a bounded scope.

        These routes reject an unbounded full-market cross-section, so the
        caller must have narrowed the request (a ts_code, or an explicit
        limit) before it can be routed here.
        """
        bounded = (PROMAX_BOUNDED_ONLY_APIS if self.get_gateway_mode == "promax"
                   else SUPER_GET_BOUNDED_ONLY_APIS)
        return api_name in bounded

    @property
    def get_verified_apis(self) -> frozenset[str]:
        return PROMAX_VERIFIED_APIS if self.get_gateway_mode == "promax" else SUPER_GET_VERIFIED_APIS

    @property
    def get_realtime_apis(self) -> frozenset[str]:
        return PROMAX_REALTIME_APIS if self.get_gateway_mode == "promax" else SUPER_GET_REALTIME_APIS


def realtime_reserved_slots(limit_per_minute: int) -> int:
    """Reserve a quarter of a provider's per-minute budget for realtime calls.

    All capability classes for one provider key still share a single rolling
    window and never exceed ``limit_per_minute`` in total (the provider's
    actual rate limit is not broadened). A bulk/daily request is only turned
    away once bulk usage alone would exceed the non-reserved remainder, so a
    burst of bulk requests (backfill, catalog fetch) cannot starve the
    latency-sensitive 1s realtime loop of every slot in the window. Below 4
    requests/minute a quarter-reservation would leave bulk with none at all,
    so no reservation applies.
    """
    limit = max(1, int(limit_per_minute))
    return max(1, limit // 4) if limit >= 4 else 0


class ProviderRateLimiter:
    """Process-local rolling-window and minimum-spacing provider guard."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._requests: dict[str, deque[float]] = {}
        self._last_started_at: dict[str, float] = {}

    async def acquire(self, provider_key: str, limit_per_minute: int, min_interval_seconds: float = 0.0,
                      *, capability_class: str = "default") -> None:
        limit = max(1, limit_per_minute)
        min_interval = max(0.0, min(60.0, min_interval_seconds))
        reserved = realtime_reserved_slots(limit) if capability_class != "realtime" else 0
        admission_limit = max(1, limit - reserved)
        while True:
            now = time.monotonic()
            async with self._lock:
                requests = self._requests.setdefault(provider_key, deque())
                while requests and now - requests[0] >= 60:
                    requests.popleft()
                window_delay = max(0.0, 60 - (now - requests[0])) if len(requests) >= admission_limit else 0.0
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


async def acquire_provider_request_slot(provider: TushareProvider, *, capability_class: str = "default") -> None:
    """Apply shared reservation first, then the existing in-process limiter."""
    reserver = _provider_request_reserver
    if reserver is not None:
        await reserver(provider.key, provider.rate_limit_per_minute, provider.min_interval_seconds)
    await request_limiter.acquire(
        provider.key, provider.rate_limit_per_minute, provider.min_interval_seconds, capability_class=capability_class,
    )


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


async def provider_http_request(provider: TushareProvider, operation: Any, *, capability_class: str = "bulk") -> httpx.Response:
    """Apply bounded retry only to transient HTTP/provider transport errors."""
    transient = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(2):
        await acquire_provider_request_slot(provider, capability_class=capability_class)
        response_headers: Any | None = None
        try:
            response = await operation()
            response_headers = response.headers
            source = f"tushare:{provider.key}"
            if 200 <= response.status_code < 400:
                network_state.record_success(source)
            elif response.status_code in transient:
                network_state.record_failure(source, f"HTTP {response.status_code}", transient=True)
        except httpx.HTTPError as error:
            network_state.record_failure(f"tushare:{provider.key}", str(error), transient=True)
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


#: Total wall-clock budget for retrying one realtime request.  Past this a
#: skipped sample is preferable to a late one; the live scan runs every 30s.
REALTIME_RETRY_DEADLINE_SECONDS = 6.0
#: Short fixed backoff between realtime retries - transient gateway rejections
#: are immediate, so a long backoff would only burn the deadline.
REALTIME_RETRY_BACKOFF_SECONDS = 0.35


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
    super_get_mode = (env.get("TUSHARE_SUPER_GET_MODE") or "legacy").strip().lower()
    if super_get_mode not in {"legacy", "promax"}:
        super_get_mode = "legacy"
    if super_get_mode == "promax":
        # ProMax is direct HTTPS.  In particular, an intentionally blank GET
        # proxy must not silently fall back to the legacy gateway proxy.
        super_realtime_key = (env.get("TUSHARE_SUPER_GET_API_KEY") or "").strip()
        super_realtime_fallback_key = (env.get("TUSHARE_SUPER_GET_FALLBACK_API_KEY") or "").strip()
        super_realtime_url = (env.get("TUSHARE_SUPER_GET_API_URL") or "").strip().rstrip("/")
        super_realtime_proxy_url = (env.get("TUSHARE_SUPER_GET_PROXY_URL") or "").strip()
    else:
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
            "super_get", "tushare_super_get",
            "Tushare ProMax GET 网关" if super_get_mode == "promax" else "Tushare 超级 GET 网关",
            super_realtime_url, super_realtime_key,
            "get_x_api_key", super_realtime_proxy_url,
            bounded_rate_limit(env.get("TUSHARE_SUPER_GET_REQUESTS_PER_MINUTE") or env.get("TUSHARE_SUPER_REALTIME_REQUESTS_PER_MINUTE"), 60),
            super_realtime_fallback_key, bounded_interval(env.get("TUSHARE_SUPER_GET_MIN_INTERVAL_SECONDS"), 1.0), super_get_mode,
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
    if api_name in PROMAX_VERIFIED_APIS:
        # Verified on ProMax but not on the legacy GET gateway.  Expanding
        # optimistically is safe because provider_candidates still filters by
        # the *configured* gateway's own verified set, so a legacy deployment
        # never routes here.  ProMax is placed second on purpose: it answers
        # roughly one call in five with a transient rejection and its latency
        # ranges from 200ms to tens of seconds, so it earns a redundancy slot,
        # not the primary one.
        return ("super_sdk", "super_get")
    return ("super_sdk",)


def provider_candidates(api_name: str, preferred: ProviderPreference = "auto", *, environ: Mapping[str, str] | None = None) -> list[TushareProvider]:
    configs = provider_configs(environ)
    requested_names = provider_order(api_name) if preferred == "auto" else (preferred,)
    names: list[ProviderName] = []
    for requested_name in requested_names:
        for name in _expand_provider_name(api_name, requested_name):
            if name not in names:
                names.append(name)
    # DataHub is an explicitly audited REST fallback.  Append it only for the
    # automatic route so an explicit provider choice remains strict and the
    # source cannot unexpectedly affect a super-only comparison.
    if preferred == "auto" and "backup" not in names:
        names.append("backup")
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
            if provider.get_gateway_mode == "promax":
                return ("verified_partial", "ProMax GET re-probed 2026-08-26 with retries: 43 of 45 candidate routes returned real rows, including the full moneyflow family, limit pools and the reporting calendar. rt_fut_min_daily stays excluded (HTTP 503 on every attempt).", sorted(provider.get_realtime_apis))
            return ("verified_partial", "Verified GET realtime: stock, ETF/index/SW snapshots and stock/futures minute subsets; unsupported live families remain excluded.", sorted(provider.get_realtime_apis))
        if provider.name == "super_sdk" and provider.configured:
            return ("verified_partial", "Verified City minute routes are timestamped. rt_k/rt_etf_k/rt_idx_k are delayed cumulative context only because no exchange timestamp was returned; *_min_daily routes remain unavailable.", sorted(SUPER_SDK_REALTIME_APIS))
        return ("not_applicable", "No realtime route is configured for this provider.", [])

    entries: list[dict[str, Any]] = []
    sdk_first_apis = SUPER_CITY_FIRST_APIS | frozenset(
        api_name for api_name, order in SUPER_REALTIME_PROVIDER_ORDER.items() if order[0] == "super_sdk"
    )
    for provider in provider_configs(environ).values():
        get_first_apis = (
            provider.get_verified_apis - SUPER_CITY_FIRST_APIS - frozenset(SUPER_REALTIME_PROVIDER_ORDER)
        ) | frozenset(
            api_name for api_name, order in SUPER_REALTIME_PROVIDER_ORDER.items()
            if order[0] == "super_get" and api_name in provider.get_verified_apis
        )
        realtime_coverage, realtime_note, verified_get_apis = realtime_summary(provider)
        entries.append({
            "name": provider.name, "provider_key": provider.key, "label": provider.label, "configured": provider.configured,
            "protocol": provider.protocol,
            "realtime_protocol": "get_x_api_key" if provider.name == "super_get" else "sdk_post" if provider.name == "super_sdk" else "none",
            "realtime_configured": provider.name in {"super_get", "super_sdk"} and provider.configured,
            "realtime_coverage": realtime_coverage,
            "realtime_note": realtime_note,
            "realtime_apis": verified_get_apis,
            "delayed_context_apis": sorted(SUPER_SDK_DELAYED_CONTEXT_APIS) if provider.name == "super_sdk" else [],
            "super_alias_first_apis": sorted(sdk_first_apis) if provider.name == "super_sdk"
                                      else sorted(get_first_apis) if provider.name == "super_get" else [],
            "get_gateway_mode": provider.get_gateway_mode if provider.name == "super_get" else None,
            "get_apis": sorted(provider.get_verified_apis) if provider.name == "super_get" else verified_get_apis,
            "datahub_apis": sorted(DATAHUB_VERIFIED_APIS) if provider.name == "backup" else [],
            "complete_query_apis": sorted(provider.get_verified_apis - SUPER_GET_BOUNDED_ONLY_APIS - SUPER_GET_RECONCILIATION_APIS) if provider.name == "super_get" else [],
            "bounded_only_apis": sorted(provider.get_verified_apis & SUPER_GET_BOUNDED_ONLY_APIS) if provider.name == "super_get" else [],
            "reconciliation_required_apis": sorted(provider.get_verified_apis & SUPER_GET_RECONCILIATION_APIS) if provider.name == "super_get" else [],
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
    # These routes accept a comma-separated batch as well as a single code, and
    # the gateway may answer a batch with extra codes.  Comparing the raw
    # parameter string against one row's ts_code discarded every row of a
    # batched response, which silently turned a working batch request into an
    # empty result.
    wanted = {part.strip() for part in requested.split(",") if part.strip()}
    matched = [row for row in rows
               if str(row.get("ts_code") or row.get("code") or "").strip().upper() in wanted]
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
        realtime_request = api_name in provider.get_realtime_apis
        # A stale realtime request is less useful than a skipped sample, but a
        # single attempt threw away far more than it protected: ProMax answers
        # roughly one call in five with a transient 503/504, and rt_min
        # accumulated 108 consecutive recorded failures under the old
        # one-attempt rule while the same call succeeded on retry when probed
        # by hand.  A transient rejection usually comes back fast (sub-second),
        # so the retry is gated on a wall-clock deadline rather than a fixed
        # count: a cheap fast failure is retried, a slow one is not, and
        # freshness is still bounded.
        attempts_per_credential = 3 if realtime_request else 2
        realtime_deadline = asyncio.get_running_loop().time() + REALTIME_RETRY_DEADLINE_SECONDS
        # ProMax accepted the same contract but its upstream account pool can
        # respond more slowly than the legacy proxy.  It is kept out of the
        # high-concurrency fast loop by deployment pacing, so allow a bounded
        # 20-second realtime probe rather than misclassifying slow valid rows
        # as a transport failure.
        request_timeout = 20 if realtime_request and provider.get_gateway_mode == "promax" else 8 if realtime_request else 15
        failures: list[str] = []
        for credential in credentials:
            for attempt in range(attempts_per_credential):
                await acquire_provider_request_slot(provider, capability_class="realtime" if realtime_request else "bulk")
                response_headers: Any | None = None
                try:
                    # The verified gateway/proxy pair requires requests-style
                    # proxy routing; httpx CONNECT receives 407 here. Reuse a
                    # thread-local Session so frequent realtime calls do not
                    # renegotiate the proxy/TLS connection on every request.
                    # Realtime attempts share one freshness budget instead of
                    # each getting the full timeout: without this a single slow
                    # attempt would consume the whole deadline and a retry
                    # could still return a quote far too late to act on.
                    attempt_timeout = request_timeout
                    if realtime_request:
                        remaining = realtime_deadline - asyncio.get_running_loop().time()
                        attempt_timeout = max(1.0, min(request_timeout, remaining))

                    def proxy_http_get() -> requests.Response:
                        return _super_get_http_get(
                            f"{provider.endpoint}/{api_name}", params=params,
                            credential=credential, proxy_url=provider.proxy_url, timeout=attempt_timeout,
                        )

                    response = await _super_get_executor_boundary.run(
                        _super_get_executor, proxy_http_get, timeout_seconds=attempt_timeout + 2,
                    )
                    response_headers = response.headers
                    source = f"tushare:{provider.key}:{provider.get_gateway_mode or 'get'}"
                    if response.ok:
                        network_state.record_success(source)
                    elif response.status_code in {408, 429, 500, 502, 503, 504}:
                        network_state.record_failure(source, f"HTTP {response.status_code}", transient=True)
                    if response.ok:
                        return _filter_requested_realtime_rows(api_name, params, _decode_rows(response.json()))
                    detail = safe_error_detail(response.text, 180)
                    failures.append(f"HTTP {response.status_code}: {detail or response.reason}")
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        break
                except requests.RequestException as error:
                    network_state.record_failure(f"tushare:{provider.key}:{provider.get_gateway_mode or 'get'}", str(error), transient=True)
                    failures.append(type(error).__name__)
                except ValueError as error:
                    failures.append(type(error).__name__)
                if attempt + 1 >= attempts_per_credential:
                    continue
                if realtime_request:
                    # Only keep retrying while a fresh sample is still possible.
                    if asyncio.get_running_loop().time() >= realtime_deadline:
                        failures.append("realtime retry deadline reached")
                        break
                    await asyncio.sleep(REALTIME_RETRY_BACKOFF_SECONDS)
                else:
                    await asyncio.sleep(retry_delay_seconds(response_headers, 0.8))
        joined_failures = f"{provider.label} failed with configured credentials: " + "; ".join(failures)
        error_cls, retry_after = classify_provider_error_text(joined_failures)
        if error_cls is ProviderRateLimitedError:
            raise ProviderRateLimitedError(joined_failures, retry_after=retry_after)
        if error_cls is ProviderUnauthorizedError:
            raise ProviderUnauthorizedError(joined_failures)
        raise ProviderCallError(joined_failures)

    # The uses_super_get branch above already returned; this covers the sdk_path
    # (super_sdk) and backup_rest (never realtime-capable) protocols.
    sdk_realtime_request = api_name in provider.get_realtime_apis
    async with provider_http_client(provider.key, provider.proxy_url) as client:
        if provider.protocol == "backup_rest":
            endpoint_name = api_name.replace("_", "-")
            provider_params = dict(params)
            if "limit" in provider_params:
                try:
                    provider_params["limit"] = min(3000, max(1, int(provider_params["limit"])))
                except (TypeError, ValueError) as error:
                    raise ProviderCallError("backup provider limit must be an integer") from error
            response = await provider_http_request(provider, lambda: client.get(
                f"{provider.endpoint}/app-api/openapi/v1/tushare/{endpoint_name}",
                headers={"X-API-Key": provider.credential}, params=provider_params,
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
        response = await provider_http_request(
            provider, lambda: client.post(endpoint, json=payload),
            capability_class="realtime" if sdk_realtime_request else "bulk",
        )
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
    joined_failures = " | ".join(f"{key}: {error}" for key, error in failures)
    error_cls, retry_after = classify_provider_error_text(joined_failures)
    if error_cls is ProviderRateLimitedError:
        raise ProviderRateLimitedError(joined_failures, tuple(failures), retry_after=retry_after)
    if error_cls is ProviderUnauthorizedError:
        raise ProviderUnauthorizedError(joined_failures, tuple(failures))
    raise ProviderCallError(joined_failures, tuple(failures))
