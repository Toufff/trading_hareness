"""Durable cross-process leases backed by the quant PostgreSQL schema."""

from __future__ import annotations

import os
import uuid
from typing import Any, Mapping


POST_CLOSE_REFRESH_LEASE_KEY = "post_close_refresh_v1"


class LeaseLostError(RuntimeError):
    """A write path detected that its captured fencing token is stale.

    Raised when a caller's lease has been superseded by a new holder (its
    fence has advanced) since this caller last confirmed ownership. The
    caller must treat any in-progress work as unsafe to commit further and
    surface this as a failure rather than continuing to write.
    """


def post_close_refresh_lease_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Bound a durable post-close lease without allowing a stale day-long lock."""
    env = os.environ if environ is None else environ
    try:
        return min(7_200, max(300, int(env.get("POST_CLOSE_REFRESH_LEASE_SECONDS", "1800"))))
    except ValueError:
        return 1_800


def background_loop_lease_seconds(environ: Mapping[str, str] | None = None) -> int:
    """Keep background-loop takeover bounded after a process loss."""
    env = os.environ if environ is None else environ
    try:
        return min(600, max(60, int(env.get("BACKGROUND_LOOP_LEASE_SECONDS", "120"))))
    except ValueError:
        return 120


def acquire_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> int | None:
    """Atomically take an expired durable lease and return its new fence.

    ``fence`` increments only here (a new ownership epoch): it stays stable
    across every later ``renew_runtime_lease`` call by the same holder, so a
    caller can capture it once and cheaply detect a stale/superseded holder
    at write time without every renewal producing a false positive.
    Returns ``None`` when the lease is currently held by someone else.
    """
    with database.transaction() as connection:
        row = connection.execute(
            """INSERT INTO quant.runtime_leases(lease_key,holder_id,acquired_at,expires_at,updated_at,fence)
               VALUES(%s,%s,now(),now() + (%s * interval '1 second'),now(),1)
               ON CONFLICT(lease_key) DO UPDATE SET holder_id=EXCLUDED.holder_id,acquired_at=now(),
                 expires_at=EXCLUDED.expires_at,updated_at=now(),fence=quant.runtime_leases.fence+1
               WHERE quant.runtime_leases.expires_at <= now()
               RETURNING fence""",
            (lease_key, holder_id, lease_seconds),
        ).fetchone()
    return int(row["fence"]) if row is not None else None


def renew_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> int | None:
    """Extend a lease only when it still belongs to the active caller.

    The fence is not incremented by a renewal; it is returned unchanged so a
    caller can confirm it still matches the value captured at acquire time.
    Returns ``None`` when the lease was lost (expired or taken by another
    holder) rather than successfully renewed.
    """
    with database.transaction() as connection:
        row = connection.execute(
            """UPDATE quant.runtime_leases
                  SET expires_at=now() + (%s * interval '1 second'),updated_at=now()
                WHERE lease_key=%s AND holder_id=%s AND expires_at > now()
              RETURNING fence""",
            (lease_seconds, lease_key, holder_id),
        ).fetchone()
    return int(row["fence"]) if row is not None else None


def release_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID) -> None:
    """Release only the caller's lease; another instance cannot be unlocked."""
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM quant.runtime_leases WHERE lease_key=%s AND holder_id=%s",
            (lease_key, holder_id),
        )


def current_runtime_lease_fence(database: Any, lease_key: str) -> int | None:
    """Read the live fence for one lease key, or ``None`` if it has no row."""
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT fence FROM quant.runtime_leases WHERE lease_key=%s", (lease_key,),
        ).fetchone()
    return int(row["fence"]) if row is not None else None


def check_runtime_lease_fence(database: Any, lease_key: str, expected_fence: int) -> None:
    """Raise :class:`LeaseLostError` when the live fence no longer matches.

    Call this at the start of a write transaction for a bounded long task
    (post-close stage, backfill, replay) so a write dispatched before a lease
    was lost cannot silently commit after a new holder has already taken
    over the same evidence tables.
    """
    live_fence = current_runtime_lease_fence(database, lease_key)
    if live_fence != expected_fence:
        raise LeaseLostError(
            f"lease {lease_key!r} fence changed from {expected_fence} to {live_fence!r}; "
            "this holder's lease was superseded",
        )


__all__ = [
    "LeaseLostError",
    "POST_CLOSE_REFRESH_LEASE_KEY",
    "background_loop_lease_seconds",
    "acquire_runtime_lease",
    "check_runtime_lease_fence",
    "current_runtime_lease_fence",
    "post_close_refresh_lease_seconds",
    "release_runtime_lease",
    "renew_runtime_lease",
]
