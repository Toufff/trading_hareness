"""Durable cross-process leases backed by the quant PostgreSQL schema."""

from __future__ import annotations

import os
import uuid
from typing import Any, Mapping


POST_CLOSE_REFRESH_LEASE_KEY = "post_close_refresh_v1"


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


def acquire_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> bool:
    """Atomically take an expired durable lease in one short transaction."""
    with database.transaction() as connection:
        row = connection.execute(
            """INSERT INTO quant.runtime_leases(lease_key,holder_id,acquired_at,expires_at,updated_at)
               VALUES(%s,%s,now(),now() + (%s * interval '1 second'),now())
               ON CONFLICT(lease_key) DO UPDATE SET holder_id=EXCLUDED.holder_id,acquired_at=now(),
                 expires_at=EXCLUDED.expires_at,updated_at=now()
               WHERE quant.runtime_leases.expires_at <= now()
               RETURNING holder_id""",
            (lease_key, holder_id, lease_seconds),
        ).fetchone()
    return row is not None


def renew_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> bool:
    """Extend a lease only when it still belongs to the active caller."""
    with database.transaction() as connection:
        row = connection.execute(
            """UPDATE quant.runtime_leases
                  SET expires_at=now() + (%s * interval '1 second'),updated_at=now()
                WHERE lease_key=%s AND holder_id=%s AND expires_at > now()
              RETURNING holder_id""",
            (lease_seconds, lease_key, holder_id),
        ).fetchone()
    return row is not None


def release_runtime_lease(database: Any, lease_key: str, holder_id: uuid.UUID) -> None:
    """Release only the caller's lease; another instance cannot be unlocked."""
    with database.transaction() as connection:
        connection.execute(
            "DELETE FROM quant.runtime_leases WHERE lease_key=%s AND holder_id=%s",
            (lease_key, holder_id),
        )


__all__ = [
    "POST_CLOSE_REFRESH_LEASE_KEY",
    "background_loop_lease_seconds",
    "acquire_runtime_lease",
    "post_close_refresh_lease_seconds",
    "release_runtime_lease",
    "renew_runtime_lease",
]
