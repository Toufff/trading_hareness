"""Native-async provider catalog, capability and health display reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .provider_catalog import project_tushare_catalog
from .provider_observability import project_provider_health
from .tushare_catalog import catalog_counts, catalog_items


async def tushare_catalog(
    async_database: Any, provider_status_fn: Callable[[], list[dict[str, Any]]],
    free_provider_status_fn: Callable[[], list[dict[str, Any]]],
) -> dict[str, Any]:
    items = [dict(item) for item in catalog_items()]
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT provider_key,api_name,availability,verified_at,last_checked_at,metadata
               FROM quant.provider_api_capabilities
               WHERE provider_key IN ('tushare_primary','tushare_super_sdk','tushare_super_get','tushare_backup')"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return project_tushare_catalog(
        items=items, rows=rows, counts=dict(catalog_counts()),
        providers=[*provider_status_fn(), *free_provider_status_fn()],
    )


async def provider_capabilities(async_database: Any) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT c.provider_key,p.label,c.api_name,c.availability,c.frequency,c.decision_eligible,c.note,c.verified_at,c.last_checked_at,c.metadata
               FROM quant.provider_api_capabilities c JOIN quant.providers p ON p.provider_key=c.provider_key
               ORDER BY c.api_name,c.provider_key"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return {"items": rows}


async def provider_health(
    async_database: Any, provider_configs: list[dict[str, Any]], observed_at: datetime,
) -> dict[str, Any]:
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT p.provider_key,p.label,p.enabled,c.capability,c.market,c.priority,c.rate_limit_per_minute,
                      h.consecutive_failures,h.circuit_open_until,h.last_success_at,h.last_failure_at,h.last_error,h.last_latency_ms,h.last_row_count
               FROM quant.providers p LEFT JOIN quant.provider_capabilities c ON c.provider_key=p.provider_key
               LEFT JOIN quant.provider_health h ON h.provider_key=c.provider_key AND h.capability=c.capability AND h.market=c.market
               ORDER BY c.capability,p.provider_key"""
        )
        rows = [dict(row) for row in await result.fetchall()]
    return project_provider_health(rows, provider_configs, observed_at)


__all__ = ["provider_capabilities", "provider_health", "tushare_catalog"]
