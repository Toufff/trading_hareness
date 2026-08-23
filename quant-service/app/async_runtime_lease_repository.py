"""Native-async durable lease primitives for long-lived runtime loops.

Each operation is one atomic PostgreSQL statement.  The synchronous lease API
remains available for the post-close orchestrator's established executor
contract; background loops use this module so concurrent startup/renewal does
not queue behind unrelated synchronous repository work.
"""

from __future__ import annotations

import uuid
from typing import Any


async def acquire(async_database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> bool:
    """Atomically acquire only an absent or expired durable lease."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """INSERT INTO quant.runtime_leases(lease_key,holder_id,acquired_at,expires_at,updated_at)
               VALUES(%s,%s,now(),now() + (%s * interval '1 second'),now())
               ON CONFLICT(lease_key) DO UPDATE SET holder_id=EXCLUDED.holder_id,acquired_at=now(),
                 expires_at=EXCLUDED.expires_at,updated_at=now()
               WHERE quant.runtime_leases.expires_at <= now()
               RETURNING holder_id""",
            (lease_key, holder_id, lease_seconds),
        )
        row = await result.fetchone()
    return row is not None


async def renew(async_database: Any, lease_key: str, holder_id: uuid.UUID, lease_seconds: int) -> bool:
    """Extend a lease only while it remains held by this caller."""
    async with async_database.transaction() as connection:
        result = await connection.execute(
            """UPDATE quant.runtime_leases
                  SET expires_at=now() + (%s * interval '1 second'),updated_at=now()
                WHERE lease_key=%s AND holder_id=%s AND expires_at > now()
              RETURNING holder_id""",
            (lease_seconds, lease_key, holder_id),
        )
        row = await result.fetchone()
    return row is not None


async def release(async_database: Any, lease_key: str, holder_id: uuid.UUID) -> None:
    """Release only the caller's lease; another holder cannot be unlocked."""
    async with async_database.transaction() as connection:
        await connection.execute(
            "DELETE FROM quant.runtime_leases WHERE lease_key=%s AND holder_id=%s",
            (lease_key, holder_id),
        )


__all__ = ["acquire", "release", "renew"]
