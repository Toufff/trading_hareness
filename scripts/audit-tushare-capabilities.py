#!/usr/bin/env python3
"""Bounded, read-only Tushare provider capability audit.

The HTTP audit route intentionally limits one request to a small batch.  This
operator tool runs the same catalog/default-parameter contract over a larger
matrix without writing fetch rows or strategy state.  It never prints tokens
or response values; the JSON artifact contains counts, fields and statuses.
Run it inside the quant-research image so the application dependencies and
configured provider credentials are used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.tushare_catalog import TUSHARE_CATALOG
from app.tushare_official import HISTORICAL_MINUTE_APIS, REALTIME_MARKET_HOURS_APIS, default_probe_params
from app.tushare_providers import call_provider, provider_configs, safe_error_detail


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _key_for_row(row: dict[str, Any]) -> tuple[str, ...] | None:
    fields = ("ts_code", "trade_date", "trade_time", "cal_date", "ann_date", "end_date", "con_code", "index_code")
    key = tuple(str(row[name]) for name in fields if row.get(name) not in (None, ""))
    return key or None


def _classify_error(error: BaseException) -> str:
    text = safe_error_detail(str(error), 240).lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if any(token in text for token in ("401", "403", "permission", "no permission", "not support", "unsupported")):
        return "unsupported"
    if any(token in text for token in ("429", "rate", "频控", "限流")):
        return "rate_limited"
    if any(token in text for token in ("parameter", "参数", "required", "必填")):
        return "invalid_parameter"
    return "transport_or_provider_error"


async def _probe(provider: Any, api_name: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        rows = await asyncio.wait_for(call_provider(provider, api_name, params, None), timeout=timeout)
        fields = sorted({str(key) for row in rows for key in row})
        keys = [_key_for_row(row) for row in rows]
        duplicate_count = len([key for key in keys if key is not None]) - len({key for key in keys if key is not None})
        return {
            "status": "valid_empty" if not rows else "verified_partial",
            "received": len(rows),
            "fields": fields,
            "duplicate_keys": max(0, duplicate_count),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "params": params,
        }
    except asyncio.TimeoutError:
        return {"status": "timeout", "error_class": "timeout", "latency_ms": round((time.perf_counter() - started) * 1000, 1), "params": params}
    except Exception as error:  # provider-specific errors are part of the matrix
        return {
            "status": "failed",
            "error_class": _classify_error(error),
            "error": safe_error_detail(str(error), 240),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "params": params,
        }


async def audit(args: argparse.Namespace) -> dict[str, Any]:
    as_of = _parse_date(args.as_of)
    providers = provider_configs()
    requested_providers = [name.strip() for name in args.providers.split(",") if name.strip()]
    unknown_providers = sorted(set(requested_providers) - set(providers))
    if unknown_providers:
        raise SystemExit(f"unknown providers: {', '.join(unknown_providers)}")
    requested_apis = list(TUSHARE_CATALOG)
    if args.apis:
        requested_apis = [name.strip() for name in args.apis.split(",") if name.strip()]
    unknown_apis = sorted(set(requested_apis) - set(TUSHARE_CATALOG))
    if unknown_apis:
        raise SystemExit(f"unknown APIs: {', '.join(unknown_apis)}")
    results: list[dict[str, Any]] = []
    for provider_name in requested_providers:
        provider = providers[provider_name]
        for api_name in requested_apis:
            params = default_probe_params(api_name, symbol=args.symbol, as_of=as_of)
            base = {"provider": provider_name, "api_name": api_name, "configured": provider.configured}
            if not provider.configured:
                results.append({**base, "status": "skipped", "reason": "provider_not_configured"})
                continue
            if not provider.supports(api_name):
                results.append({**base, "status": "unsupported", "reason": "provider_allowlist"})
                continue
            if api_name in HISTORICAL_MINUTE_APIS:
                results.append({**base, "status": "skipped", "reason": "offline_files_only"})
                continue
            if api_name in REALTIME_MARKET_HOURS_APIS:
                results.append({**base, "status": "skipped", "reason": "market_hours_only"})
                continue
            if params is None:
                results.append({**base, "status": "untested", "reason": "manual_parameters_required"})
                continue
            if not args.live:
                results.append({**base, "status": "ready", "params": params})
                continue
            results.append({**base, **await _probe(provider, api_name, params, args.timeout)})
            # DataHub has a six-per-minute deployment budget; spacing is kept
            # explicit so an audit cannot accidentally become a load test.
            if provider_name == "backup" and args.delay_seconds > 0:
                await asyncio.sleep(args.delay_seconds)
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "as_of": args.as_of,
        "symbol": args.symbol,
        "live": args.live,
        "catalog_count": len(TUSHARE_CATALOG),
        "provider_count": len(requested_providers),
        "counts": counts,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--providers", default="primary,super_sdk,super_get,backup")
    parser.add_argument("--apis", help="comma-separated API names; defaults to the complete catalog")
    parser.add_argument("--symbol", default="000636.SZ")
    parser.add_argument("--as-of", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--delay-seconds", type=float, default=10.0)
    parser.add_argument("--live", action="store_true", help="make bounded read-only provider calls")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = asyncio.run(audit(args))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
