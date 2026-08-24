"""Read-only process and persistent-storage resource diagnostics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


GIB = 1024 ** 3
# The operator reserved 40 GiB for the complete research estate.  Daily P2
# history is a database-resident evidence ledger, so reserve 36 GiB for that
# hot path and 4 GiB for bounded artifacts/exports.  The total 40 GiB cap is
# still absolute; retention jobs keep their existing bounded windows.
DEFAULT_RESEARCH_STORAGE_SOFT_BYTES = 40 * GIB
DEFAULT_HOT_DATABASE_SOFT_BYTES = 36 * GIB


def bounded_min_free_bytes(value: str | None) -> int:
    try:
        return max(128 * 1024 ** 2, min(20 * GIB, int(value or GIB)))
    except ValueError:
        return GIB


def bounded_warning_free_bytes(value: str | None, min_free_bytes: int) -> int:
    """Keep a disk-warning watermark above the hard capture-stop floor.

    The edge has a small root volume. A warning must leave enough room for an
    operator to inspect and release space before the capture floor is reached;
    it must never silently exceed the bounded runtime allocation.
    """
    try:
        configured = int(value or 10 * GIB)
    except ValueError:
        configured = 10 * GIB
    return max(int(min_free_bytes), min(20 * GIB, configured))


def bounded_memory_ratio(value: str | None) -> float:
    try:
        return max(0.5, min(0.98, float(value or "0.85")))
    except ValueError:
        return 0.85


def bounded_storage_budget_bytes(value: str | None, default: int, maximum: int) -> int:
    """Read a storage budget inside the operator-approved allocation.

    A configuration value is an admission-control preference, not permission
    to grow the research estate past its approved capacity.  In particular,
    callers must pass the total or hot-database allocation as ``maximum`` so a
    stale environment file cannot silently turn the 40 GiB plan into an
    unbounded collection job.
    """
    try:
        return max(GIB, min(int(maximum), int(value or default)))
    except ValueError:
        return min(int(default), int(maximum))


def bounded_storage_ratio(value: str | None, default: float) -> float:
    """Keep warning/stop watermarks ordered and operationally meaningful."""
    try:
        return max(0.5, min(0.98, float(value or default)))
    except ValueError:
        return default


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
                           warning_free_bytes: int | None = None,
                           rss_bytes: int | None, memory_limit_bytes: int | None,
                           max_memory_ratio: float) -> tuple[str, list[str]]:
    degraded_reasons: list[str] = []
    if disk_free_bytes < min_free_bytes:
        degraded_reasons.append("persistent storage free space is below the configured floor")
    if rss_bytes is not None and memory_limit_bytes is not None and memory_limit_bytes > 0:
        if rss_bytes / memory_limit_bytes >= max_memory_ratio:
            degraded_reasons.append("process RSS is above the configured cgroup memory ratio")
    if degraded_reasons:
        return "degraded", degraded_reasons
    if warning_free_bytes is not None and disk_free_bytes < warning_free_bytes:
        return "warning", ["persistent storage free space is below the configured warning watermark"]
    return "healthy", []


def research_storage_governance(*, hot_database_bytes: int, artifact_bytes: int,
                                research_budget_bytes: int, hot_database_budget_bytes: int,
                                warning_ratio: float, stop_ratio: float) -> dict[str, Any]:
    """Classify bounded research storage without deleting any evidence.

    The hot PostgreSQL schema is the scarce path for high-frequency evidence,
    so it has its own smaller budget.  The aggregate budget includes it plus
    locally managed research artifacts.  At the stop watermark callers must
    skip *nonessential* high-frequency capture, never delete records or stop
    watched-price/risk evaluation.
    """
    used_bytes = max(0, int(hot_database_bytes)) + max(0, int(artifact_bytes))
    hot_ratio = hot_database_bytes / hot_database_budget_bytes if hot_database_budget_bytes else 1.0
    total_ratio = used_bytes / research_budget_bytes if research_budget_bytes else 1.0
    warning = hot_ratio >= warning_ratio or total_ratio >= warning_ratio
    stop = hot_ratio >= stop_ratio or total_ratio >= stop_ratio
    reasons: list[str] = []
    if hot_ratio >= stop_ratio:
        reasons.append("quant hot database reached the high-frequency stop watermark")
    elif hot_ratio >= warning_ratio:
        reasons.append("quant hot database reached the warning watermark")
    if total_ratio >= stop_ratio:
        reasons.append("managed research storage reached the stop watermark")
    elif total_ratio >= warning_ratio:
        reasons.append("managed research storage reached the warning watermark")
    return {
        "state": "stop_nonessential_high_frequency" if stop else "warning" if warning else "healthy",
        "reasons": reasons,
        "allow_nonessential_high_frequency": not stop,
        "hot_database": {"used_bytes": int(hot_database_bytes), "budget_bytes": int(hot_database_budget_bytes),
                         "ratio": round(hot_ratio, 6)},
        "artifacts": {"used_bytes": int(artifact_bytes)},
        "managed": {"used_bytes": used_bytes, "budget_bytes": int(research_budget_bytes),
                    "ratio": round(total_ratio, 6), "warning_ratio": warning_ratio, "stop_ratio": stop_ratio},
    }


def managed_directory_bytes(storage_path: Path) -> int:
    """Return regular-file bytes below the local research directory, bounded to local files.

    Symlinks and disappearing files are ignored deliberately: this metric is
    only a conservative admission control signal, not a filesystem inventory.
    """
    total = 0
    try:
        for entry in storage_path.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def runtime_resource_status(storage_path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(storage_path)
    min_free_bytes = bounded_min_free_bytes(os.getenv("QUANT_RUNTIME_MIN_FREE_BYTES"))
    warning_free_bytes = bounded_warning_free_bytes(
        os.getenv("QUANT_RUNTIME_WARNING_FREE_BYTES"), min_free_bytes,
    )
    max_memory_ratio = bounded_memory_ratio(os.getenv("QUANT_RUNTIME_MAX_MEMORY_RATIO"))
    rss_bytes = process_rss_bytes()
    memory_limit_bytes = cgroup_memory_limit_bytes()
    state, reasons = runtime_resource_state(
        disk_free_bytes=usage.free, min_free_bytes=min_free_bytes, warning_free_bytes=warning_free_bytes,
        rss_bytes=rss_bytes,
        memory_limit_bytes=memory_limit_bytes, max_memory_ratio=max_memory_ratio,
    )
    return {
        "state": state, "reasons": reasons, "storage_path": str(storage_path),
        "disk": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free,
                 "free_ratio": round(usage.free / usage.total, 6) if usage.total else None,
                 "min_free_bytes": min_free_bytes, "warning_free_bytes": warning_free_bytes},
        "memory": {"rss_bytes": rss_bytes, "cgroup_limit_bytes": memory_limit_bytes,
                   "max_ratio": max_memory_ratio,
                   "ratio": round(rss_bytes / memory_limit_bytes, 6)
                   if rss_bytes is not None and memory_limit_bytes else None},
    }
