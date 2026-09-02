"""Native-async durable lease primitives for long-lived runtime loops.

Each operation is one atomic PostgreSQL statement.  The synchronous lease API
remains available for the post-close orchestrator's established executor
contract; background loops use this module so concurrent startup/renewal does
not queue behind unrelated synchronous repository work.
"""

from __future__ import annotations

import uuid
from typing import Any

from .runtime_leases import LeaseLostError


async def acquire(async_database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> int | None:
    """Atomically acquire only an absent or expired durable lease.

    Returns the lease's new fence (see ``runtime_leases.acquire_runtime_lease``
    for the fencing-token contract shared by both the sync and async lease
    primitives), or ``None`` when the lease is currently held elsewhere.
    """
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """INSERT INTO quant.runtime_leases(lease_key,holder_id,acquired_at,expires_at,updated_at,fence)
               VALUES(%s,%s,now(),now() + (%s * interval '1 second'),now(),1)
               ON CONFLICT(lease_key) DO UPDATE SET holder_id=EXCLUDED.holder_id,acquired_at=now(),
                 expires_at=EXCLUDED.expires_at,updated_at=now(),fence=quant.runtime_leases.fence+1
               WHERE quant.runtime_leases.expires_at <= now()
               RETURNING fence""",
            (lease_key, holder_id, lease_seconds),
        )
        row = await result.fetchone()
    return int(row["fence"]) if row is not None else None


async def renew(async_database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> int | None:
    """Extend a lease only while it remains held by this caller.

    The fence is not incremented by a renewal; it is returned unchanged.
    """
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """UPDATE quant.runtime_leases
                  SET expires_at=now() + (%s * interval '1 second'),updated_at=now()
                WHERE lease_key=%s AND holder_id=%s AND expires_at > now()
              RETURNING fence""",
            (lease_seconds, lease_key, holder_id),
        )
        row = await result.fetchone()
    return int(row["fence"]) if row is not None else None


async def release(async_database: Any, lease_key: str, holder_id: uuid.UUID) -> None:
    """Release only the caller's lease; another holder cannot be unlocked."""
    async with async_database.transaction() as connection:
        await connection.execute(
            "DELETE FROM quant.runtime_leases WHERE lease_key=%s AND holder_id=%s",
            (lease_key, holder_id),
        )


async def current_fence(async_database: Any, lease_key: str) -> int | None:
    """Read the live fence for one lease key, or ``None`` if it has no row."""
    async with async_database.transaction() as connection:
        result = await connection.execute("SELECT fence FROM quant.runtime_leases WHERE lease_key=%s", (lease_key,))
        row = await result.fetchone()
    return int(row["fence"]) if row is not None else None


async def check_fence(async_database: Any, lease_key: str, expected_fence: int) -> None:
    """Raise :class:`~.runtime_leases.LeaseLostError` when the fence moved on.

    Call this at the start of an async write for a bounded long task so a
    write dispatched before a lease was lost cannot silently commit after a
    new holder has already taken over the same evidence tables.
    """
    live_fence = await current_fence(async_database, lease_key)
    if live_fence != expected_fence:
        raise LeaseLostError(
            f"lease {lease_key!r} fence changed from {expected_fence} to {live_fence!r}; "
            "this holder's lease was superseded",
        )


__all__ = ["acquire", "check_fence", "current_fence", "release", "renew"]
