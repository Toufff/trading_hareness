"""Native-async projection for persisted analyst language-skill profiles.

The profile builder remains an offline write workflow.  This module only
returns its immutable, already-persisted output to the dashboard, so a busy
analyst view cannot consume a legacy blocking database-executor slot.
"""

from __future__ import annotations

from typing import Any

from .analyst_skill_models import SKILL_MODEL_VERSION


async def profiles(async_database: Any, analyst_id: str | None, limit: int) -> dict[str, Any]:
    """Return bounded stored profiles without rebuilding or contacting a source."""
    bounded_limit = max(1, min(int(limit), 100))
    async with async_database.transaction() as connection:
        if analyst_id:
            result = await connection.execute(
                """SELECT remote_analyst_id,as_of_date,model_version,status,profile,created_at,updated_at
                     FROM quant.analyst_skill_profiles WHERE remote_analyst_id=%s
                     ORDER BY as_of_date DESC LIMIT %s""",
                (analyst_id, bounded_limit),
            )
        else:
            result = await connection.execute(
                """SELECT DISTINCT ON(remote_analyst_id) remote_analyst_id,as_of_date,model_version,status,profile,created_at,updated_at
                     FROM quant.analyst_skill_profiles ORDER BY remote_analyst_id,as_of_date DESC LIMIT %s""",
                (bounded_limit,),
            )
        rows = [dict(row) for row in await result.fetchall()]
    return {
        "items": rows,
        "model_version": SKILL_MODEL_VERSION,
        "data_boundary": "offline text-only distillation; profiles and prompt variants cannot change live rules",
    }


__all__ = ["profiles"]
