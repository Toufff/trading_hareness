"""Native-async local provider-circuit projections.

Circuit state is local durable evidence, so these reads never contact a
provider.  Keeping them on the async pool avoids consuming a blocking-executor
slot before a caller decides whether an upstream request is permitted.
"""

from __future__ import annotations

from typing import Any


async def open_capabilities(async_database: Any, provider_key: str, capabilities: list[str]) -> set[str]:
    if not capabilities:
        return set()
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT capability FROM quant.provider_health
                 WHERE provider_key=%s AND market='cn' AND capability=ANY(%s)
                   AND circuit_open_until IS NOT NULL AND circuit_open_until > now()""",
            (provider_key, capabilities),
        )
        rows = await result.fetchall()
    return {str(row["capability"]) for row in rows}


async def open_provider_keys(async_database: Any, capability: str, provider_keys: list[str]) -> set[str]:
    if not provider_keys:
        return set()
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """SELECT provider_key FROM quant.provider_health
                 WHERE capability=%s AND market='cn' AND provider_key=ANY(%s)
                   AND circuit_open_until IS NOT NULL AND circuit_open_until > now()""",
            (capability, provider_keys),
        )
        rows = await result.fetchall()
    return {str(row["provider_key"]) for row in rows}


__all__ = ["open_capabilities", "open_provider_keys"]
