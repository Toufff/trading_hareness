"""Startup runtime for local provider capability and limiter projections.

It materializes declared catalog contracts and the process-effective limiter
configuration, but never contacts a provider or stores credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def mirror_runtime_rate_limits(connection: Any, configs: Mapping[str, Any]) -> None:
    """Mirror effective local rate limits into the read-only control plane."""
    for provider in configs.values():
        rate_limit = int(provider.rate_limit_per_minute)
        provider_key = str(provider.key)
        connection.execute(
            """UPDATE quant.provider_capabilities SET rate_limit_per_minute=%s
                 WHERE provider_key=%s AND market='cn'""",
            (rate_limit, provider_key),
        )
        connection.execute(
            """UPDATE quant.providers
                  SET config=config || jsonb_build_object(
                        'rate_limit_source','runtime_environment',
                        'runtime_rate_limit_per_minute',%s
                      ),updated_at=now()
                 WHERE provider_key=%s""",
            (rate_limit, provider_key),
        )


@dataclass(frozen=True)
class ProviderControlPlaneRuntimeDependencies:
    database: Any
    provider_configs: Callable[[], Mapping[str, Any]]
    catalog_items: Callable[[], list[dict[str, Any]]]
    capability_contract: Callable[[str], Any]
    super_get_verified_apis: frozenset[str]
    json_value: Callable[[Any], Any]


class ProviderControlPlaneRuntime:
    """Project declared capabilities and active local limits in one transaction."""

    def __init__(self, dependencies: ProviderControlPlaneRuntimeDependencies) -> None:
        self._dependencies = dependencies

    def initialize(self) -> None:
        dependencies = self._dependencies
        items = dependencies.catalog_items()
        configs = dependencies.provider_configs()
        with dependencies.database.transaction() as connection:
            mirror_runtime_rate_limits(connection, configs)
            for item in items:
                api_name = str(item["api_name"])
                contract = dependencies.capability_contract(api_name)
                provider_keys = ["tushare_primary", "tushare_super_sdk"]
                if api_name in dependencies.super_get_verified_apis:
                    provider_keys.append("tushare_super_get")
                if api_name == "stock_basic":
                    provider_keys.append("tushare_backup")
                metadata = {
                    "catalog_origin": item["catalog_origin"],
                    "permission_model": item["permission_model"],
                    "min_points": item["min_points"],
                    "request_policy": item["request_policy"],
                    "model_role": item["model_role"],
                    "priority": item["priority"],
                }
                for provider_key in provider_keys:
                    connection.execute(
                        """INSERT INTO quant.provider_api_capabilities(provider_key,api_name,availability,frequency,decision_eligible,note,metadata)
                           VALUES(%s,%s,'declared',%s,%s,%s,%s)
                           ON CONFLICT(provider_key,api_name) DO UPDATE SET frequency=EXCLUDED.frequency,
                             decision_eligible=EXCLUDED.decision_eligible,
                             metadata=quant.provider_api_capabilities.metadata || EXCLUDED.metadata""",
                        (
                            provider_key,
                            api_name,
                            contract.frequency,
                            contract.decision_eligible,
                            contract.note[:500],
                            dependencies.json_value(metadata),
                        ),
                    )


__all__ = [
    "ProviderControlPlaneRuntime",
    "ProviderControlPlaneRuntimeDependencies",
    "mirror_runtime_rate_limits",
]
