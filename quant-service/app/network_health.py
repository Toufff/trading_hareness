"""Passive network reachability state for the locally hosted service.

The tracker never performs a probe (and therefore never consumes provider
quota).  It is updated by real outbound requests and is deliberately separate
from provider permission health: a 403 means a provider/configuration issue,
not that the machine is offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from threading import Lock
from typing import Any


_SECRET = re.compile(r"(?i)(authorization|x-api-key|api[_-]?key|token)\s*[:=]\s*[^\s,;&]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(value: str) -> str:
    compact = str(value).replace("\n", " ").strip()
    compact = _SECRET.sub(lambda match: f"{match.group(1)}=<redacted>", compact)
    compact = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", compact)
    return compact[:240]


@dataclass
class NetworkStateTracker:
    failure_threshold: int = 3
    _lock: Lock = field(default_factory=Lock, repr=False)
    _state: str = "unknown"
    _consecutive_failures: int = 0
    _last_success_at: str | None = None
    _last_failure_at: str | None = None
    _changed_at: str | None = None
    _last_source: str | None = None
    _last_error: str | None = None
    _recovery_count: int = 0

    def __post_init__(self) -> None:
        self._update_metrics()

    def _update_metrics(self) -> None:
        # Lazy import keeps this pure state object usable in provider/unit
        # tests without forcing Prometheus collection during module import.
        from .telemetry import network_consecutive_failures, network_reachability
        states = ("unknown", "degraded", "offline", "recovering", "online")
        for state in states:
            network_reachability.labels(state).set(1 if self._state == state else 0)
        network_consecutive_failures.set(self._consecutive_failures)

    def record_success(self, source: str, latency_ms: int | None = None) -> None:
        with self._lock:
            was_offline = self._state == "offline"
            self._state = "recovering" if was_offline else "online"
            self._consecutive_failures = 0
            self._last_success_at = _now()
            self._last_source = source[:80]
            self._last_error = None
            if was_offline:
                self._recovery_count += 1
                self._changed_at = self._last_success_at
                from .telemetry import network_recoveries_total
                network_recoveries_total.inc()
            self._update_metrics()

    def record_failure(self, source: str, error: str, *, transient: bool = True) -> None:
        if not transient:
            return
        with self._lock:
            now = _now()
            self._consecutive_failures += 1
            self._last_failure_at = now
            self._last_source = source[:80]
            self._last_error = _safe_error(error)
            next_state = "offline" if self._consecutive_failures >= self.failure_threshold else "degraded"
            if next_state != self._state:
                self._changed_at = now
            self._state = next_state
            self._update_metrics()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "changed_at": self._changed_at,
                "last_source": self._last_source,
                "last_error": self._last_error,
                "recovery_count": self._recovery_count,
                "mode": "passive_request_observation",
            }


network_state = NetworkStateTracker()


__all__ = ["NetworkStateTracker", "network_state"]
