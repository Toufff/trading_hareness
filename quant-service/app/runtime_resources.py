"""Read-only process and persistent-storage resource diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


GIB = 1024 ** 3


def bounded_min_free_bytes(value: str | None) -> int:
    try:
        return max(128 * 1024 ** 2, min(20 * GIB, int(value or GIB)))
    except ValueError:
        return GIB


def bounded_memory_ratio(value: str | None) -> float:
    try:
        return max(0.5, min(0.98, float(value or "0.85")))
    except ValueError:
        return 0.85


def cgroup_memory_limit_bytes() -> int | None:
    """Return a cgroup-v2 memory ceiling when one is actually configured."""
    try:
        value = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        return int(value) if value and value != "max" else None
    except (OSError, ValueError):
        return None


def process_rss_bytes() -> int | None:
    """Read Linux RSS without adding a runtime dependency such as psutil."""
    try:
        fields = Path("/proc/self/statm").read_text().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def runtime_resource_state(*, disk_free_bytes: int, min_free_bytes: int,
                           rss_bytes: int | None, memory_limit_bytes: int | None,
                           max_memory_ratio: float) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if disk_free_bytes < min_free_bytes:
        reasons.append("persistent storage free space is below the configured floor")
    if rss_bytes is not None and memory_limit_bytes is not None and memory_limit_bytes > 0:
        if rss_bytes / memory_limit_bytes >= max_memory_ratio:
            reasons.append("process RSS is above the configured cgroup memory ratio")
    return ("degraded" if reasons else "healthy"), reasons


def runtime_resource_status(storage_path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(storage_path)
    min_free_bytes = bounded_min_free_bytes(os.getenv("QUANT_RUNTIME_MIN_FREE_BYTES"))
    max_memory_ratio = bounded_memory_ratio(os.getenv("QUANT_RUNTIME_MAX_MEMORY_RATIO"))
    rss_bytes = process_rss_bytes()
    memory_limit_bytes = cgroup_memory_limit_bytes()
    state, reasons = runtime_resource_state(
        disk_free_bytes=usage.free, min_free_bytes=min_free_bytes, rss_bytes=rss_bytes,
        memory_limit_bytes=memory_limit_bytes, max_memory_ratio=max_memory_ratio,
    )
    return {
        "state": state, "reasons": reasons, "storage_path": str(storage_path),
        "disk": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                 "free_ratio": round(usage.free / usage.total, 6) if usage.total else None,
                 "min_free_bytes": min_free_bytes},
        "memory": {"rss_bytes": rss_bytes, "cgroup_limit_bytes": memory_limit_bytes,
                   "max_ratio": max_memory_ratio,
                   "ratio": round(rss_bytes / memory_limit_bytes, 6)
                   if rss_bytes is not None and memory_limit_bytes else None},
    }
