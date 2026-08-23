"""Local storage budget measurement and optional-capture admission control."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from .runtime_resources import (
    DEFAULT_HOT_DATABASE_SOFT_BYTES,
    DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
    bounded_storage_budget_bytes,
    bounded_storage_ratio,
    managed_directory_bytes,
    research_storage_governance,
)


def governance(
    database: Any,
    *,
    environ: Mapping[str, str] | None = None,
    directory_bytes: Callable[[Path], int] = managed_directory_bytes,
) -> dict[str, Any]:
    """Measure managed research storage without mutating or pruning evidence."""
    env = os.environ if environ is None else environ
    with database.transaction() as connection:
        row = connection.execute(
            """SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint AS bytes
                 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='quant' AND c.relkind IN ('r','m','p')""",
        ).fetchone()
    data_dir = Path(env.get("QUANT_DATA_DIR", "/var/lib/quant"))
    warning_ratio = bounded_storage_ratio(env.get("QUANT_RESEARCH_STORAGE_WARNING_RATIO"), 0.80)
    return research_storage_governance(
        hot_database_bytes=int((row or {}).get("bytes") or 0),
        artifact_bytes=directory_bytes(data_dir),
        research_budget_bytes=bounded_storage_budget_bytes(
            env.get("QUANT_RESEARCH_STORAGE_SOFT_BYTES"), DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
            DEFAULT_RESEARCH_STORAGE_SOFT_BYTES,
        ),
        hot_database_budget_bytes=bounded_storage_budget_bytes(
            env.get("QUANT_HOT_DATABASE_SOFT_BYTES"), DEFAULT_HOT_DATABASE_SOFT_BYTES,
            DEFAULT_HOT_DATABASE_SOFT_BYTES,
        ),
        warning_ratio=warning_ratio,
        stop_ratio=max(bounded_storage_ratio(env.get("QUANT_RESEARCH_STORAGE_STOP_RATIO"), 0.90), warning_ratio),
    )


@dataclass
class ResearchStorageAdmission:
    """Cache only the local budget decision; never gate core safety paths."""

    status_fn: Callable[[], dict[str, Any]]
    run_database: Callable[..., Awaitable[dict[str, Any]]]
    cache_seconds: float = 60.0
    _cache: tuple[float, dict[str, Any]] | None = None

    async def optional_high_frequency_allowed(self) -> tuple[bool, dict[str, Any]]:
        now = asyncio.get_running_loop().time()
        cached = self._cache
        if cached is None or now - cached[0] >= self.cache_seconds:
            status = await self.run_database(self.status_fn, timeout_seconds=10)
            self._cache = (now, status)
        else:
            status = cached[1]
        return bool(status.get("allow_nonessential_high_frequency", True)), status


__all__ = ["ResearchStorageAdmission", "governance"]
