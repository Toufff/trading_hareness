"""Dependency-injected post-close refresh orchestration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException


async def run_refresh(
    request: Any,
    *,
    db: Any,
    lease_key: str,
    lease_seconds: Callable[[], int],
    run_database_blocking: Callable[..., Awaitable[Any]],
    acquire_lease: Callable[..., Any],
    renew_lease: Callable[..., Any],
    release_lease: Callable[..., Any],
    actions: dict[str, Callable[[], Any]],
    stage_order: tuple[str, ...],
    trade_date: date,
    safe_error_detail: Callable[[str, int], str],
    json_safe: Callable[[Any], Any],
    timeout_overrides: dict[str, float] | None = None,
    record_stage: Callable[[str, date, Callable[[], Any]], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Run durable post-close stages in their existing dependency order.

    The orchestrator owns only lease/timeout/result semantics.  Provider and
    persistence actions are injected by the service, so this module cannot
    silently widen the data-fetch or historical-data boundary.
    """
    lease_holder_id = uuid.uuid4()
    acquired = await run_database_blocking(acquire_lease, db, lease_key, lease_holder_id, lease_seconds())
    if not acquired:
        raise HTTPException(status_code=409, detail="a post-close refresh is already running in another service instance")

    started_at = datetime.now(timezone.utc)
    stages: dict[str, dict[str, Any]] = {}
    limits = timeout_overrides or {}

    async def stage(name: str) -> dict[str, Any]:
        phase_started = asyncio.get_running_loop().time()
        timeout_seconds = float(limits.get(name, 90.0))
        try:
            if record_stage is not None:
                result = record_stage(name, trade_date, actions[name])
            else:
                result = actions[name]()
            if hasattr(result, "__await__"):
                result = await asyncio.wait_for(result, timeout=timeout_seconds)
            payload = dict(result) if isinstance(result, dict) else {"result": result}
            payload.setdefault("status", "completed")
        except asyncio.TimeoutError:
            payload = {"status": "failed", "error": f"stage exceeded its {int(timeout_seconds)}s budget; retry later"}
        except Exception as error:  # noqa: BLE001 - later evidence remains useful
            payload = {"status": "failed", "error": safe_error_detail(str(error), 500)}
        payload["latency_ms"] = round((asyncio.get_running_loop().time() - phase_started) * 1000)
        stages[name] = json_safe(payload)
        renewed = await run_database_blocking(renew_lease, db, lease_key, lease_holder_id, lease_seconds())
        if not renewed:
            raise RuntimeError("post-close refresh lease was lost; remaining stages were not run")
        return payload

    try:
        for name in stage_order:
            if name == "core_daily_controls":
                stages[name] = {
                    "status": "skipped",
                    "reason": "per-symbol control-plane sync remains in the watched-stock workflow; full-market daily is authoritative here",
                    "latency_ms": 0,
                }
                continue
            await stage(name)
        sources = {
            "tushare_super": "requested through daily, THS flow, limit ladder, specialty and index phases",
            "akshare_eastmoney": "requested through supplements and board review",
            "tencent": "requested through the close all-A snapshot and board review",
            "cninfo": stages.get("cninfo_announcements", {"status": "skipped"})["status"],
            "sina": "not used for full-market close; bounded stock-study fallback only",
            "xinhua_finance": "skipped: no licensed endpoint/authentication configured",
        }
        deferred = [name for name, item in stages.items() if item.get("status") in {"blocked", "failed"}]
        daily = stages.get("full_market_daily", {"status": "blocked"})
        daily_ready = daily.get("status") in {"completed", "unchanged"}
        return {
            "status": "completed" if not deferred else "partial", "trade_date": str(trade_date),
            "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
            "daily_ready": daily_ready, "deferred_stages": deferred,
            "retry_hint": None if daily_ready else "收盘日线尚未发布时，可稍后再次点击；自动盘后任务也会在18:55-19:10重试策略筛选。",
            "sources": sources, "stages": stages,
            "notice": "一键更新只保存研究证据和候选，不会自动下单或发送交易指令。",
        }
    finally:
        try:
            await run_database_blocking(release_lease, db, lease_key, lease_holder_id)
        except Exception as error:  # noqa: BLE001 - lease expiry remains a safe recovery path
            print(f"post-close refresh lease release failed: {safe_error_detail(str(error), 300)}")


__all__ = ["run_refresh"]
