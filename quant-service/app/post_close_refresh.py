"""Dependency-injected post-close refresh orchestration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from .automation_run_repository import fail_run, finish_run, start_or_resume_run
from .logging_config import get_logger
from .runtime_leases import LeaseLostError


logger = get_logger(__name__)


POST_CLOSE_RECEIPT_VERSION = "post-close-refresh-v5"


async def record_stage_with_receipt(
    name: str,
    trade_date: date,
    action: Callable[[], Any],
    *,
    db: Any,
    run_database_blocking: Callable[..., Awaitable[Any]],
    safe_error_detail: Callable[[str, int], str],
) -> Any:
    """Run one stage only when its durable receipt is not already complete."""
    # The version belongs in the idempotency key, not only metadata.  A stage
    # implementation change must not be hidden forever behind an older
    # completed receipt.
    run_key = f"{POST_CLOSE_RECEIPT_VERSION}:{name}:{trade_date}"

    def begin() -> dict[str, Any]:
        with db.transaction() as connection:
            return start_or_resume_run(
                connection, task_key="post_close_refresh.stage", run_key=run_key,
                cadence="daily", as_of_date=trade_date,
                methodology_version=POST_CLOSE_RECEIPT_VERSION, input_summary={"stage": name},
            )

    receipt = await run_database_blocking(begin, timeout_seconds=10)
    if receipt.get("status") == "completed":
        summary = dict(receipt.get("output_summary") or {})
        # Older receipts could persist ``{"status": null}`` when an action
        # omitted its own status even though the durable run was completed.
        # The receipt row is authoritative here; never let a null summary
        # re-block every downstream stage on every later restart.
        summary["status"] = "completed"
        summary["resumed_from_receipt"] = True
        return summary

    run_id = receipt["run_id"]
    try:
        result = action()
        if hasattr(result, "__await__"):
            result = await result
    except Exception as error:
        # ``except ... as error`` is implicitly deleted once the block exits;
        # capture it before defining a lambda closure over it so a deferred
        # call cannot see a NameError instead of the real failure.
        caught_error = error
        await run_database_blocking(
            lambda: _fail_stage_receipt(db, run_id, caught_error, safe_error_detail), timeout_seconds=10,
        )
        raise

    status = result.get("status") if isinstance(result, dict) else "completed"
    if status not in {"completed", "partial", "blocked", "failed"}:
        status = "completed"
    await run_database_blocking(
        lambda: _finish_stage_receipt(db, run_id, status, result), timeout_seconds=10,
    )
    return result


def _fail_stage_receipt(db: Any, run_id: str, error: BaseException,
                        safe_error_detail: Callable[[str, int], str]) -> None:
    with db.transaction() as connection:
        fail_run(connection, run_id, RuntimeError(safe_error_detail(str(error), 500)))


def _finish_stage_receipt(db: Any, run_id: str, status: str, result: Any) -> None:
    with db.transaction() as connection:
        finish_run(
            connection, run_id, status=status,
            # Persist the normalized status computed by the wrapper rather
            # than trusting every legacy action to return one consistently.
            output_summary={"status": status},
        )


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
    stage_dependencies: dict[str, tuple[str, ...]] | None = None,
    record_stage: Callable[[str, date, Callable[[], Any]], Awaitable[Any]] | None = None,
    check_lease_fence: Callable[[Any, str, int], Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Run durable post-close stages in their existing dependency order.

    The orchestrator owns only lease/timeout/result semantics.  Provider and
    persistence actions are injected by the service, so this module cannot
    silently widen the data-fetch or historical-data boundary.
    """
    lease_holder_id = uuid.uuid4()
    # ``acquire_lease`` now returns a fencing token (see runtime_leases.py):
    # a monotonic counter that only advances when a *new* holder takes the
    # lease, and stays stable across this holder's own renewals. Capturing
    # it once here lets every stage below cheaply detect a stale/superseded
    # holder before it starts writing, instead of only noticing after the
    # fact via a failed renewal at the end of a stage.
    lease_fence = await run_database_blocking(acquire_lease, db, lease_key, lease_holder_id, lease_seconds())
    if not lease_fence:
        raise HTTPException(status_code=409, detail="a post-close refresh is already running in another service instance")

    started_at = datetime.now(timezone.utc)
    stages: dict[str, dict[str, Any]] = {}
    limits = timeout_overrides or {}
    dependencies = stage_dependencies or {}

    # An independent heartbeat renews the lease on its own clock rather than
    # only between stages. A single stage close to (or past) its own timeout
    # budget must not be able to run right up against the lease's own TTL
    # (audit: "analyst_outcomes timeout 300s == 最小 lease 300s") with no
    # renewal happening while it is in flight; this task keeps ticking
    # concurrently with whatever the current stage is awaiting.
    heartbeat_lost = asyncio.Event()
    heartbeat_error: BaseException | None = None

    async def heartbeat() -> None:
        nonlocal heartbeat_error
        interval = max(1.0, lease_seconds() / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed_fence = await run_database_blocking(renew_lease, db, lease_key, lease_holder_id, lease_seconds())
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - conservatively stop before an unverified lease expires
                heartbeat_error = error
                heartbeat_lost.set()
                return
            if not renewed_fence or renewed_fence != lease_fence:
                heartbeat_error = RuntimeError("post-close refresh lease was lost during heartbeat renewal")
                heartbeat_lost.set()
                return

    heartbeat_task = asyncio.create_task(heartbeat())

    async def stage(name: str) -> dict[str, Any]:
        phase_started = asyncio.get_running_loop().time()
        timeout_seconds = float(limits.get(name, 90.0))
        blocked_by = [
            dependency for dependency in dependencies.get(name, ())
            if stages.get(dependency, {}).get("status") not in {"completed", "unchanged"}
        ]
        if blocked_by:
            payload = {
                "status": "blocked",
                "reason": f"required post-close stage unavailable: {', '.join(blocked_by)}",
            }
        else:
            try:
                # A write dispatched to the bounded executor cannot be
                # cancelled once its thread has started, so this check alone
                # cannot stop an already-running stage's commit; it does stop
                # every *later* stage from starting once a takeover is
                # detected, narrowing the window from "the whole run" to "one
                # in-flight stage" (see runtime_leases.check_runtime_lease_fence).
                if check_lease_fence is not None:
                    await check_lease_fence(db, lease_key, lease_fence)
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
            except LeaseLostError:
                # The lease itself is gone; continuing to the next stage
                # would let this holder keep acting as though it still owned
                # the refresh. Abort the whole run rather than one stage.
                raise
            except Exception as error:  # noqa: BLE001 - later evidence remains useful
                payload = {"status": "failed", "error": safe_error_detail(str(error), 500)}
        payload["latency_ms"] = round((asyncio.get_running_loop().time() - phase_started) * 1000)
        stages[name] = json_safe(payload)
        renewed_fence = await run_database_blocking(renew_lease, db, lease_key, lease_holder_id, lease_seconds())
        # A renewal never changes the fence; if it comes back different from
        # what this holder captured at acquire time, something else already
        # re-acquired and this holder's view of ownership cannot be trusted.
        if not renewed_fence or renewed_fence != lease_fence:
            raise RuntimeError("post-close refresh lease was lost; remaining stages were not run")
        return payload

    try:
        for name in stage_order:
            if heartbeat_lost.is_set():
                raise heartbeat_error or RuntimeError("post-close refresh lease was lost")
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
        controls = stages.get("core_daily_controls", {"status": "blocked"})
        controls_ready = controls.get("status") in {"completed", "unchanged"}
        retry_hint = (
            "收盘日线尚未发布时，可稍后再次点击；自动盘后任务也会在18:55-19:10重试策略筛选。"
            if not daily_ready else
            "日线控制面尚未完整，依赖复权、涨跌停或停牌字段的策略阶段已阻断；可稍后重试。"
            if not controls_ready else None
        )
        return {
            "status": "completed" if not deferred else "partial", "trade_date": str(trade_date),
            "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
            "daily_ready": daily_ready, "controls_ready": controls_ready, "deferred_stages": deferred,
            "retry_hint": retry_hint,
            "sources": sources, "stages": stages,
            "notice": "一键更新只保存研究证据和候选，不会自动下单或发送交易指令。",
        }
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        try:
            await run_database_blocking(release_lease, db, lease_key, lease_holder_id)
        except Exception as error:  # noqa: BLE001 - lease expiry remains a safe recovery path
            logger.warning(
                f"post-close refresh lease release failed: {safe_error_detail(str(error), 300)}",
                extra={"task": "post_close_refresh"},
            )


__all__ = ["POST_CLOSE_RECEIPT_VERSION", "record_stage_with_receipt", "run_refresh"]
