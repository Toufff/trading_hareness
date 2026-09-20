"""Read-only process and persistent-storage resource diagnostics."""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


GIB = 1024 ** 3
# The 40 GiB / 36 GiB pair these numbers replace was sized in 2026-08, when the
# whole cluster still shared the G: HDD with reports and backups.  The hot data
# directory now has a dedicated NVMe volume governed by its own guard
# (``PGDATA_BUDGET_BYTES``, 500 GB, enforced by ``scripts/database-storage-tiers.py``
# with an 85% high-water mark), so this budget is a *sub*-allocation of that
# tier, not the estate ceiling it used to be.  300 GiB of the 500 GB tier leaves
# room for WAL, temp files and an index rebuild, and keeps the tiering job as
# the real backstop.  Callers pass these as the ``maximum`` of
# :func:`bounded_storage_budget_bytes`, so an environment file can only lower
# them -- raising the ceiling means editing this line.
DEFAULT_RESEARCH_STORAGE_SOFT_BYTES = 320 * GIB
DEFAULT_HOT_DATABASE_SOFT_BYTES = 300 * GIB

#: Rows moved here by the tiering job leave the NVMe tier for the G: HDD.
COLD_TABLESPACE = "stock_cold"

#: Bytes the ``quant`` schema occupies **on the hot tier**.
#:
#: The cold twins and ``legacy_source_records`` live in the same schema but on
#: another volume, so counting them charged ~2.6 GiB of HDD to an NVMe budget
#: and -- worse -- made the number immune to the one job that exists to lower
#: it: tiering moves rows from ``t`` to ``t_cold``, both in ``quant``, so an
#: unfiltered ``sum(pg_total_relation_size)`` does not move by a single byte.
#: Every caller must use this statement; two call sites drifting apart is what
#: ``test_historical_backfill_enforces_the_same_hot_database_budget_as_runtime_health``
#: exists to prevent.
HOT_DATABASE_BYTES_SQL = """
SELECT coalesce(sum(pg_total_relation_size(c.oid)), 0)::bigint AS bytes
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_tablespace ts ON ts.oid = c.reltablespace
 WHERE n.nspname = 'quant'
   AND c.relkind IN ('r', 'm', 'p')
   AND coalesce(ts.spcname, '') <> %s
"""


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
                                warning_ratio: float, stop_ratio: float,
                                artifact_measurement: DirectoryMeasurement | None = None) -> dict[str, Any]:
    """Classify bounded research storage without deleting any evidence.

    The hot PostgreSQL schema is the scarce path for high-frequency evidence,
    so it has its own smaller budget.  The aggregate budget includes it plus
    locally managed research artifacts.  At the stop watermark callers must
    skip *nonessential* high-frequency capture, never delete records or stop
    watched-price/risk evaluation.

    ``artifact_measurement`` does not change ``artifact_bytes`` -- it only says
    *when* that number was measured, so a reader can tell a fresh walk from a
    cached one (see :class:`ManagedDirectoryCache`).
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
        "artifacts": {"used_bytes": int(artifact_bytes),
                      **(artifact_measurement.freshness() if artifact_measurement else {})},
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


#: How long one artifact-store measurement may answer for.  The number is an
#: admission-control signal against a 4 GiB allocation, so a minute of drift
#: cannot move any watermark decision; a 4.5 s walk on every ``/health`` can
#: and did.
MANAGED_DIRECTORY_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class DirectoryMeasurement:
    """One directory-size measurement and how old it is."""

    used_bytes: int
    #: When the walk that produced ``used_bytes`` finished, ISO-8601 UTC.
    measured_at: str
    age_seconds: float
    #: True when this answer came from the cache instead of a fresh walk.
    cached: bool
    ttl_seconds: float

    def freshness(self) -> dict[str, Any]:
        """The part a health payload publishes next to the byte count."""
        return {"measured_at": self.measured_at, "age_seconds": round(self.age_seconds, 3),
                "cached": self.cached, "ttl_seconds": self.ttl_seconds}


class ManagedDirectoryCache:
    """Process-local, TTL-bounded artifact-store measurements.

    ``managed_directory_bytes`` walks every file below the research directory.
    ``/health`` asked for that on every single request, and on this host's HDD
    the walk measured 4.3-4.9 s, which is long enough to break a release's
    health probes -- a liveness check was being answered by a filesystem
    inventory.

    The fix is the smallest one that keeps the number honest: cache it for
    :data:`MANAGED_DIRECTORY_CACHE_SECONDS` and publish how old it is.  The
    number's *meaning* is unchanged -- it is still regular-file bytes below the
    directory, measured exactly the same way -- so no watermark, ratio or
    admission decision shifts.  What changes is that a reader can now see
    whether they are looking at a fresh walk, and a caller that needs one can
    ask for it (``force=True``), which is the size-on-demand path for anything
    that must decide against the current size rather than the recent one.

    The clock is injectable so the TTL can be tested without sleeping, and the
    walk is injectable so it can be tested without a filesystem.  A lock keeps
    two concurrent ``/health`` requests from both walking the same directory.
    """

    def __init__(self, *, ttl_seconds: float = MANAGED_DIRECTORY_CACHE_SECONDS,
                 walk: Callable[[Path], int] = managed_directory_bytes,
                 monotonic: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] | None = None) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._walk = walk
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, tuple[float, str, int]] = {}
        self._lock = threading.Lock()

    def measure(self, storage_path: Path, *, force: bool = False) -> DirectoryMeasurement:
        """Return the directory's size, walking it only when the TTL expired."""
        key = str(storage_path)
        now = self._monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and not force and now - entry[0] < self.ttl_seconds:
                return DirectoryMeasurement(
                    used_bytes=entry[2], measured_at=entry[1], age_seconds=max(0.0, now - entry[0]),
                    cached=True, ttl_seconds=self.ttl_seconds)
        # The walk is deliberately OUTSIDE the lock: it is the slow part, and a
        # second caller arriving mid-walk should be allowed to serve the
        # previous answer rather than queue behind a filesystem scan.
        used_bytes = int(self._walk(Path(storage_path)))
        measured_at = self._wall_clock().isoformat()
        finished = self._monotonic()
        with self._lock:
            self._entries[key] = (finished, measured_at, used_bytes)
        return DirectoryMeasurement(used_bytes=used_bytes, measured_at=measured_at,
                                    age_seconds=0.0, cached=False, ttl_seconds=self.ttl_seconds)

    def invalidate(self, storage_path: Path | None = None) -> None:
        """Drop one path's cached measurement, or all of them."""
        with self._lock:
            if storage_path is None:
                self._entries.clear()
            else:
                self._entries.pop(str(storage_path), None)


#: The one cache ``/health`` and the admission check share, so a walk paid for
#: by one of them answers the other too.
managed_directory_cache = ManagedDirectoryCache()


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
