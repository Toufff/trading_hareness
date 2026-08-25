"""Fuyao (同花顺) transport and conservative normalized snapshot adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from .fuyao_catalog import FUYAO_PATHS
from .http_clients import provider_http_client
from .tushare_providers import safe_error_detail


FUYAO_BASE_URL = "https://fuyao.aicubes.cn"
FUYAO_PROVIDER_KEY = "fuyao_ths"


class FuyaoProviderError(RuntimeError):
    """Token-free supplier failure."""


def configured() -> bool:
    return bool((os.getenv("FUYAO_API_KEY") or "").strip())


def validate_capability_query(capability: str, params: dict[str, Any]) -> str:
    path = FUYAO_PATHS.get(capability)
    if path is None:
        raise FuyaoProviderError("unknown Fuyao capability")
    if any(not isinstance(value, (str, int, float, bool)) and value is not None for value in params.values()):
        raise FuyaoProviderError("Fuyao query values must be scalar")
    return path


async def fetch(capability: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call one documented, allowlisted REST capability without leaking a key."""
    key = (os.getenv("FUYAO_API_KEY") or "").strip()
    if not key:
        raise FuyaoProviderError("FUYAO_API_KEY is not configured")
    safe_params = dict(params or {})
    path = validate_capability_query(capability, safe_params)
    try:
        async with provider_http_client(FUYAO_PROVIDER_KEY, "") as client:
            response = await client.get(f"{FUYAO_BASE_URL}{path}", params=safe_params, headers={"X-api-key": key})
            response.raise_for_status()
            payload = response.json()
    except Exception as error:  # noqa: BLE001
        raise FuyaoProviderError(safe_error_detail(str(error), 300)) from error
    if not isinstance(payload, dict):
        raise FuyaoProviderError("Fuyao returned a non-object response")
    if payload.get("code") != 0:
        raise FuyaoProviderError(safe_error_detail(str(payload.get("message") or "Fuyao business error"), 300))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FuyaoProviderError("Fuyao success response has no data object")
    return data


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


__all__ = ["FUYAO_PROVIDER_KEY", "FuyaoProviderError", "all_a_snapshot_rows", "configured", "fetch", "normalize_snapshot_rows", "validate_capability_query"]
