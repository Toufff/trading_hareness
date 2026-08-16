"""Bounded one-minute Eastmoney board-flow capture orchestration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .akshare_provider import akshare_eastmoney_board_flow
from .provider_health import record_provider_failure, record_provider_success
from .runtime_executors import ExecutorSaturatedError
from .tushare_providers import safe_error_detail


class BoardFlowCaptureActions:
    """Capture same-source board flow with bounded provider and DB work."""

    def __init__(self, database: Any) -> None:
        self._database = database

    async def capture(
        self,
        *,
        run_database: Callable[..., Awaitable[Any]],
        run_akshare: Callable[..., Awaitable[Any]],
        provider_capabilities: Callable[[str, list[str]], Awaitable[set[str]]],
        normalize_items: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]],
        persist_feature: Callable[..., dict[str, Any]],
        evaluate_rotation: Callable[[datetime, datetime], list[dict[str, Any]]],
        retry_rotation_deliveries: Callable[[], Awaitable[dict[str, int]]],
    ) -> dict[str, Any]:
        """Append one industry/concept point; no stock-level joins or alerts."""
        observed_at = datetime.now(timezone.utc)
        local = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
        snapshot_minute = local.replace(second=0, microsecond=0).astimezone(timezone.utc)
        started_at = asyncio.get_running_loop().time()
        kinds = ("concept", "industry")
        capability_for_kind = {kind: f"intraday_board_flow_{kind}" for kind in kinds}
        open_capabilities = await provider_capabilities("eastmoney_free", list(capability_for_kind.values()))
        requested_kinds = [kind for kind in kinds if capability_for_kind[kind] not in open_capabilities]
        requested_results = await asyncio.gather(
            *(run_akshare(akshare_eastmoney_board_flow, kind, timeout_seconds=20) for kind in requested_kinds),
            return_exceptions=True,
        )
        results = dict(zip(requested_kinds, requested_results, strict=True))
        items: list[dict[str, Any]] = []
        coverage: dict[str, dict[str, int]] = {}
        source_status: dict[str, dict[str, Any]] = {}
        failures = circuit_skips = capacity_blocks = 0

        def record_outcome(
            capability: str, rows: int, error: str | None = None, latency_ms: int | None = None,
        ) -> None:
            with self._database.transaction() as connection:
                if error:
                    record_provider_failure(connection, "eastmoney_free", capability, error, latency_ms)
                else:
                    record_provider_success(connection, "eastmoney_free", capability, rows, latency_ms)

        for kind in kinds:
            capability = capability_for_kind[kind]
            if capability in open_capabilities:
                circuit_skips += 1
                coverage[kind] = {"flow_boards": 0}
                source_status[kind] = {
                    "status": "circuit_open", "notice": "provider health circuit is open; upstream request skipped",
                }
                continue
            result = results[kind]
            if isinstance(result, ExecutorSaturatedError):
                capacity_blocks += 1
                coverage[kind] = {"flow_boards": 0}
                source_status[kind] = {"status": "local_capacity", "notice": safe_error_detail(str(result), 300)}
                continue
            if isinstance(result, Exception):
                failures += 1
                coverage[kind] = {"flow_boards": 0}
                detail = safe_error_detail(str(result), 300)
                source_status[kind] = {"status": "failed", "error": detail}
                await run_database(
                    record_outcome, capability, 0, detail,
                    round((asyncio.get_running_loop().time() - started_at) * 1000),
                )
                continue
            normalized = normalize_items(kind, result)
            items.extend(normalized)
            coverage[kind] = {"flow_boards": len(normalized)}
            source_status[kind] = {
                "status": "completed", "upstream_rows": len(result), "stored_boards": len(normalized),
            }
            await run_database(
                record_outcome, capability, len(normalized), None,
                round((asyncio.get_running_loop().time() - started_at) * 1000),
            )
        status = (
            "blocked" if circuit_skips + capacity_blocks == len(kinds) else
            "partial" if circuit_skips or capacity_blocks or failures == 1 else
            "completed" if failures == 0 else "failed"
        )
        payload = {
            "items": items, "rank_by": "eastmoney_net_inflow", "unit": "100m_cny",
            "missing_value_policy": "missing_is_not_zero",
        }

        def persist_snapshot() -> None:
            with self._database.transaction() as connection:
                connection.execute(
                    """INSERT INTO quant.intraday_board_flow_snapshots(
                             snapshot_minute,observed_at,status,coverage,source_status,payload)
                       VALUES(%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(snapshot_minute) DO UPDATE SET
                         observed_at=EXCLUDED.observed_at,status=EXCLUDED.status,coverage=EXCLUDED.coverage,
                         source_status=EXCLUDED.source_status,payload=EXCLUDED.payload
                       WHERE coalesce((EXCLUDED.coverage->'concept'->>'flow_boards')::int,0)
                               +coalesce((EXCLUDED.coverage->'industry'->>'flow_boards')::int,0)
                             >=coalesce((quant.intraday_board_flow_snapshots.coverage->'concept'->>'flow_boards')::int,0)
                               +coalesce((quant.intraday_board_flow_snapshots.coverage->'industry'->>'flow_boards')::int,0)""",
                    (snapshot_minute, observed_at, status, Json(coverage), Json(source_status), Json(payload)),
                )

        await run_database(persist_snapshot)
        market_flow_feature = await run_database(
            persist_feature, self._database, snapshot_minute, observed_at,
        )
        rotation_events = await run_database(evaluate_rotation, snapshot_minute, observed_at)
        retry_summary = await retry_rotation_deliveries()
        # Rotation evidence belongs to the frontend only, never chat delivery.
        rotation_deliveries = [
            {
                "rotation_event_id": str(event["rotation_event_id"]),
                "delivery": {"status": "suppressed", "reason": "frontend evidence only"},
            }
            for event in rotation_events
        ]
        return {
            "status": status, "observed_at": observed_at.isoformat(), "snapshot_minute": snapshot_minute.isoformat(),
            "coverage": coverage, "items": len(items), "circuit_skips": circuit_skips,
            "capacity_blocks": capacity_blocks, "market_flow_feature": market_flow_feature,
            "rotation": {"confirmed": len(rotation_events), "deliveries": rotation_deliveries, "retry": retry_summary},
            "latency_ms": round((asyncio.get_running_loop().time() - started_at) * 1000),
        }
