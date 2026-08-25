"""Fuyao (同花顺) transport and conservative normalized snapshot adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any

import httpx

from .fuyao_catalog import FUYAO_PATHS, FUYAO_QUERY_PARAMS
from .http_clients import provider_http_client
from .http_retry import retry_delay_seconds
from .tushare_providers import safe_error_detail


FUYAO_BASE_URL = "https://fuyao.aicubes.cn"
FUYAO_PROVIDER_KEY = "fuyao_ths"
FUYAO_API_KEY_ENV_NAMES = ("HITHINK_FINANCE_API_KEY", "FUYAO_API_KEY", "FUYAO_TOKEN")
FUYAO_RETRY_BUSINESS_CODES = {4001, 5001, 5002, 5003}
FUYAO_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
FUYAO_MAX_ATTEMPTS = 3


class FuyaoProviderError(RuntimeError):
    """Token-free supplier failure."""

    def __init__(self, message: str, *, code: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class FuyaoQueryValidationError(FuyaoProviderError):
    """The local allow-list rejected a query before provider I/O."""


def _api_key() -> str:
    for name in FUYAO_API_KEY_ENV_NAMES:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def configured() -> bool:
    return bool(_api_key())


def validate_capability_query(capability: str, params: dict[str, Any]) -> str:
    path = FUYAO_PATHS.get(capability)
    if path is None:
        raise FuyaoQueryValidationError("unknown Fuyao capability")
    if any(not isinstance(value, (str, int, float, bool)) and value is not None for value in params.values()):
        raise FuyaoQueryValidationError("Fuyao query values must be scalar")
    contract = FUYAO_QUERY_PARAMS[capability]
    unknown = sorted(set(params) - set(contract["allowed"]))
    if unknown:
        raise FuyaoQueryValidationError(f"unknown Fuyao query parameters: {', '.join(unknown)}")
    missing = sorted(
        name for name in contract["required"]
        if name not in params or params[name] is None or (isinstance(params[name], str) and not params[name].strip())
    )
    if missing:
        raise FuyaoQueryValidationError(f"missing Fuyao query parameters: {', '.join(missing)}")
    return path


async def fetch_envelope(capability: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one allowlisted route and retain the official diagnostic envelope."""
    key = _api_key()
    if not key:
        raise FuyaoProviderError("HITHINK_FINANCE_API_KEY or FUYAO_API_KEY is not configured")
    safe_params = {name: value for name, value in dict(params or {}).items() if value is not None}
    path = validate_capability_query(capability, safe_params)
    last_transport_error: Exception | None = None
    async with provider_http_client(FUYAO_PROVIDER_KEY, "") as client:
        for attempt in range(FUYAO_MAX_ATTEMPTS):
            response_headers: Any | None = None
            try:
                response = await client.get(
                    f"{FUYAO_BASE_URL}{path}", params=safe_params, headers={"X-api-key": key},
                )
                response_headers = response.headers
                if response.status_code in FUYAO_TRANSIENT_HTTP_STATUSES:
                    if attempt < FUYAO_MAX_ATTEMPTS - 1:
                        await asyncio.sleep(retry_delay_seconds(response_headers, 0.5 * (2**attempt)))
                        continue
                response.raise_for_status()
                payload = response.json()
            except (httpx.TimeoutException, httpx.TransportError) as error:
                last_transport_error = error
                if attempt < FUYAO_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(retry_delay_seconds(response_headers, 0.5 * (2**attempt)))
                    continue
                raise FuyaoProviderError(
                    safe_error_detail(f"Fuyao transport failed after {FUYAO_MAX_ATTEMPTS} attempts: {error}", 300),
                ) from error
            except (httpx.HTTPStatusError, ValueError) as error:
                raise FuyaoProviderError(safe_error_detail(str(error), 300)) from error

            if not isinstance(payload, dict):
                raise FuyaoProviderError("Fuyao returned a non-object response")
            raw_code = payload.get("code")
            try:
                code = int(raw_code)
            except (TypeError, ValueError):
                raise FuyaoProviderError("Fuyao response has no numeric business code") from None
            request_id = str(payload.get("request_id") or "").strip() or None
            message = safe_error_detail(str(payload.get("message") or ""), 300)
            if code in FUYAO_RETRY_BUSINESS_CODES and attempt < FUYAO_MAX_ATTEMPTS - 1:
                await asyncio.sleep(retry_delay_seconds(response_headers, 0.5 * (2**attempt)))
                continue
            if code != 0:
                raise FuyaoProviderError(message or "Fuyao business error", code=code, request_id=request_id)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise FuyaoProviderError(
                    "Fuyao success response has no data object", code=code, request_id=request_id,
                )
            return {"code": code, "message": message, "request_id": request_id, "data": data}
    raise FuyaoProviderError(
        safe_error_detail(f"Fuyao transport failed: {last_transport_error}", 300),
    )


async def fetch(capability: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compatibility data-only view over :func:`fetch_envelope`."""
    return dict((await fetch_envelope(capability, params))["data"])


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_snapshot_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize official price fields; intentionally do not invent fund flow."""
    timestamp = data.get("timestamp")
    try:
        observed_at = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc).isoformat() if timestamp else None
    except (TypeError, ValueError, OSError):
        observed_at = None
    result: list[dict[str, Any]] = []
    for raw in data.get("item") or []:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("thscode") or "").upper()
        if len(symbol) != 9 or symbol[-3:] not in {".SH", ".SZ", ".BJ"}:
            continue
        price = _number(raw.get("last_price"))
        if price is None or price <= 0:
            continue
        result.append({
            "symbol": symbol, "price": price, "pct_change": _number(raw.get("price_change_ratio_pct")),
            "turnover": _number(raw.get("turnover")), "volume": _number(raw.get("volume")),
            "price_source": "fuyao_ths_all_a_snapshot", "price_observed_at": observed_at,
            "raw": dict(raw),
        })
    return result


async def all_a_snapshot_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve the documented full cross-section in at most two bounded pages."""
    first = await fetch("a_share_prices_snapshot", {"limit": 5000, "offset": 0})
    rows = normalize_snapshot_rows(first)
    total = int(first.get("total") or len(rows))
    if total > 5000:
        second = await fetch("a_share_prices_snapshot", {"limit": min(5000, total - 5000), "offset": 5000})
        rows.extend(normalize_snapshot_rows(second))
    timestamp = first.get("timestamp")
    return rows, {
        "status": "fresh", "age_seconds": 0.0, "source": "fuyao_ths_all_a_snapshot",
        "scope": "all_a_cross_section", "cross_sectional": True,
        "semantics": "all_a_price_volume_turnover_snapshot_no_main_flow", "upstream_timestamp_ms": timestamp,
        "total": total, "matched_rows": len(rows),
    }


__all__ = [
    "FUYAO_API_KEY_ENV_NAMES", "FUYAO_PROVIDER_KEY", "FuyaoProviderError", "FuyaoQueryValidationError",
    "all_a_snapshot_rows", "configured", "fetch", "fetch_envelope",
    "normalize_snapshot_rows", "validate_capability_query",
]
