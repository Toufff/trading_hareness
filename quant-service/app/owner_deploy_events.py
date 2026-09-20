"""Write and read the deployment announcements external consumers depend on.

See migration 20260920_0107 for why the announcement lives in the database:
it is the only surface a release does not restart.

Two rules shape this module:

* **Announcing must never fail a deploy.**  ``record`` returns a result object
  instead of raising, so a publish whose announcement could not be written
  still proceeds -- loudly.  The alternative, a release blocked by its own
  bookkeeping, is worse than an unannounced one.
* **A phase is a fact, not a plan.**  ``starting`` is written before anything
  is touched and is never updated; ``completed``/``failed`` are separate rows.
  A publish killed mid-flight therefore leaves a ``starting`` with no terminal
  row, which is exactly what happened and what a consumer should see.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PHASES = ("starting", "completed", "failed")

#: Surfaces a deploy can interrupt, with the measured 2026-09-20 windows.  They
#: are published so a consumer can decide whether to pause or simply retry.
SURFACE_HTTP_API = "http_api"
SURFACE_SHARED_TUNNEL = "shared_tunnel"

TYPICAL_SECONDS = {SURFACE_HTTP_API: 10, SURFACE_SHARED_TUNNEL: 7}

_INSERT_SQL = """
INSERT INTO quant.owner_deploy_events
       (deploy_id, phase, release_id, git_sha, surfaces, expected_seconds, note)
VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
RETURNING event_id, recorded_at
"""

_LATEST_SQL = """
SELECT deploy_id, phase, release_id, git_sha, surfaces, expected_seconds, note, recorded_at
  FROM quant.owner_deploy_events
 ORDER BY event_id DESC
 LIMIT %s
"""


@dataclass(frozen=True)
class RecordResult:
    """What happened when we tried to announce.  Never an exception."""

    recorded: bool
    phase: str
    deploy_id: str
    event_id: int | None = None
    recorded_at: datetime | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded": self.recorded,
            "phase": self.phase,
            "deploy_id": self.deploy_id,
            "event_id": self.event_id,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "error": self.error,
        }


def expected_seconds(surfaces: dict[str, bool]) -> int:
    """The window a consumer should expect, from the surfaces this deploy touches."""
    return sum(seconds for name, seconds in TYPICAL_SECONDS.items() if surfaces.get(name))


def record(
    database: Any,
    *,
    deploy_id: str,
    phase: str,
    release_id: str,
    git_sha: str | None = None,
    surfaces: dict[str, bool] | None = None,
    note: str | None = None,
) -> RecordResult:
    """Append one phase row.  Returns a failure result rather than raising."""
    if phase not in PHASES:
        return RecordResult(False, phase, deploy_id, error=f"unknown phase {phase!r}")
    touched = dict(surfaces or {})
    try:
        with database.transaction() as connection:
            row = connection.execute(_INSERT_SQL, (
                deploy_id, phase, release_id, git_sha, json.dumps(touched),
                expected_seconds(touched) or None, note,
            )).fetchone()
    except Exception as error:  # noqa: BLE001 - announcing must not fail the deploy
        return RecordResult(False, phase, deploy_id, error=f"{type(error).__name__}: {error}"[:300])

    event_id = row["event_id"] if isinstance(row, dict) else row[0]
    recorded_at = row["recorded_at"] if isinstance(row, dict) else row[1]
    return RecordResult(True, phase, deploy_id, event_id=event_id, recorded_at=recorded_at)


def latest(connection: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    """The newest announcements, newest first."""
    rows = connection.execute(_LATEST_SQL, (max(1, min(int(limit), 200)),)).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        values = row if isinstance(row, dict) else dict(zip(
            ("deploy_id", "phase", "release_id", "git_sha", "surfaces",
             "expected_seconds", "note", "recorded_at"), row))
        recorded_at = values.get("recorded_at")
        items.append({
            "deploy_id": values.get("deploy_id"),
            "phase": values.get("phase"),
            "release_id": values.get("release_id"),
            "git_sha": values.get("git_sha"),
            "surfaces": values.get("surfaces") or {},
            "expected_seconds": values.get("expected_seconds"),
            "note": values.get("note"),
            "recorded_at": recorded_at.isoformat() if hasattr(recorded_at, "isoformat") else recorded_at,
        })
    return items


def in_progress(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The deploy that announced ``starting`` and has no terminal row yet.

    Reading the newest rows is enough: phases of one deploy are contiguous
    because a publish holds the release lock for its whole run.
    """
    terminal: set[str] = set()
    for item in events:
        if item["phase"] in ("completed", "failed"):
            terminal.add(item["deploy_id"])
        elif item["phase"] == "starting" and item["deploy_id"] not in terminal:
            return item
    return None


def new_deploy_id(moment: datetime | None = None) -> str:
    stamp = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return stamp.strftime("deploy-%Y%m%dT%H%M%SZ")


__all__ = [
    "PHASES",
    "RecordResult",
    "SURFACE_HTTP_API",
    "SURFACE_SHARED_TUNNEL",
    "TYPICAL_SECONDS",
    "expected_seconds",
    "in_progress",
    "latest",
    "new_deploy_id",
    "record",
]
