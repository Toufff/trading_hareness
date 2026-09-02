"""Native-async cross-schema projection for analyst-sync operational health."""

from __future__ import annotations

from typing import Any

from .analyst_sync_health_projection import (
    ATTEMPTS_SQL,
    CURSORS_SQL,
    GLOBAL_CURSORS_SQL,
    PROMOTION_SQL,
    WORKFLOW_SQL,
    project_sync_health,
)


async def sync_health(async_database: Any) -> dict[str, Any]:
    """Read quant receipts and optional n8n audit rows without a sync worker."""
    async with async_database.transaction() as connection:
        cursors_result = await connection.execute(CURSORS_SQL)
        global_cursors_result = await connection.execute(GLOBAL_CURSORS_SQL)
        attempts_result = await connection.execute(ATTEMPTS_SQL)
        promotion_result = await connection.execute(PROMOTION_SQL)
        cursors = await cursors_result.fetchall()
        global_cursors = await global_cursors_result.fetchall()
        attempts = await attempts_result.fetchall()
        promotion = await promotion_result.fetchall()
        try:
            # A plain ``except Exception`` around a failed statement leaves the
            # surrounding transaction aborted: PostgreSQL silently turns the
            # eventual COMMIT into a ROLLBACK, discarding the reads captured
            # above even though they already succeeded.  Running the optional
            # n8n-audit read inside its own nested ``connection.transaction()``
            # uses a SAVEPOINT instead, so a failure here only rolls back to
            # that savepoint and leaves the outer transaction committable.
            async with connection.transaction():
                workflow_result = await connection.execute(WORKFLOW_SQL)
                workflow_rows = await workflow_result.fetchall()
        except Exception:
            # An isolated quant schema has no n8n public audit tables.  The
            # durable local receipts remain useful and keep the projection
            # read-only rather than turning the whole status board into 500.
            workflow_rows = []
    return project_sync_health(cursors, global_cursors, attempts, promotion, workflow_rows)


__all__ = ["sync_health"]
