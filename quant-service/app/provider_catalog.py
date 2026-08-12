"""Read-only provider catalog and capability presentation services."""

from __future__ import annotations

from typing import Any, Callable

from .free_market_providers import free_provider_status
from .tushare_catalog import TUSHARE_CATALOG, catalog_counts, catalog_items
from .tushare_providers import SUPER_GET_VERIFIED_APIS, provider_status


def tushare_catalog_snapshot(
    database: Any,
    *,
    catalog_items_fn: Callable[[], list[dict[str, Any]]] = catalog_items,
    catalog_counts_fn: Callable[[], dict[str, Any]] = catalog_counts,
    provider_status_fn: Callable[[], list[dict[str, Any]]] = provider_status,
    free_provider_status_fn: Callable[[], list[dict[str, Any]]] = free_provider_status,
) -> dict[str, Any]:
    """Join declared catalog contracts with persisted, per-provider observations."""
    items = [dict(item) for item in catalog_items_fn()]
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT provider_key,api_name,availability,verified_at,last_checked_at,metadata
               FROM quant.provider_api_capabilities
               WHERE provider_key IN ('tushare_primary','tushare_super_sdk','tushare_super_get','tushare_backup')"""
        ).fetchall()
    observations: dict[str, dict[str, Any]] = {}
    for row in rows:
        observations.setdefault(str(row["api_name"]), {})[str(row["provider_key"])] = {
            "availability": row["availability"], "verified_at": row["verified_at"],
            "last_checked_at": row["last_checked_at"],
            "last_row_count": (row["metadata"] or {}).get("last_row_count"),
            "last_observation": (row["metadata"] or {}).get("last_observation"),
        }
    for item in items:
        provider_observations = observations.get(str(item["api_name"]), {})
        item["provider_observations"] = provider_observations
        item["primary_availability"] = provider_observations.get("tushare_primary", {}).get("availability", "unknown")
        item["super_sdk_availability"] = provider_observations.get("tushare_super_sdk", {}).get("availability", "unknown")
        item["super_get_availability"] = provider_observations.get("tushare_super_get", {}).get("availability", "unknown")
        item["super_availability"] = (
            item["super_get_availability"] if str(item["api_name"]) in SUPER_GET_VERIFIED_APIS
            else item["super_sdk_availability"]
        )
    counts = dict(catalog_counts_fn())
    for prefix, key in (("primary", "primary_availability"), ("super", "super_availability"),
                        ("super_get", "super_get_availability"), ("super_sdk", "super_sdk_availability")):
        counts[f"{prefix}_verified"] = sum(item[key] == "verified" for item in items)
        counts[f"{prefix}_responded"] = sum(item[key] in {"verified", "empty"} for item in items)
    return {
        "items": items, "count": len(TUSHARE_CATALOG), "counts": counts, "online_range_max_days": 45,
        "historical_minute_policy": "offline_files_only",
        "realtime_minute_policy": "market_hours_single_symbol_probe_before_decision_use",
        "coverage_rule": "catalog declaration != provider verification; only non-header market rows produce verified",
        "providers": [*provider_status_fn(), *free_provider_status_fn()],
    }


def provider_capabilities_snapshot(database: Any) -> dict[str, Any]:
    """Return the persisted cross-provider capability matrix without polling."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT c.provider_key,p.label,c.api_name,c.availability,c.frequency,c.decision_eligible,c.note,c.verified_at,c.last_checked_at,c.metadata
               FROM quant.provider_api_capabilities c JOIN quant.providers p ON p.provider_key=c.provider_key
               ORDER BY c.api_name,c.provider_key"""
        ).fetchall()
    return {"items": rows}
