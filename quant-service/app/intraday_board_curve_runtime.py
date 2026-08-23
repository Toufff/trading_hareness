"""Application adapter for one-minute board-flow curve retention.

The scheduler owns Shanghai-time minute cadence and no-catch-up behavior.  This
adapter owns only the source-local retention transactions, executed through the
bounded database executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class IntradayBoardCurveRuntimeDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    board_session: Callable[[], Awaitable[tuple[bool, str]]]
    storage_allowed: Callable[[], Awaitable[tuple[bool, dict[str, Any]]]]
    capture: Callable[[], Awaitable[dict[str, Any]]]
    curve_retention_days: Callable[[], int]
    rotation_retention_days: Callable[[], int]
    run_loop: Callable[..., Awaitable[None]]


async def run_intraday_board_curve_runtime_loop(
    dependencies: IntradayBoardCurveRuntimeDependencies,
) -> None:
    """Run board-flow collection without direct sync database work in main."""
    async def prune_before(now: datetime, curve_days: int, rotation_days: int) -> None:
        cutoff = now - timedelta(days=curve_days)
        rotation_cutoff = now - timedelta(days=rotation_days)

        def prune() -> None:
            with dependencies.database.transaction() as connection:
                connection.execute(
                    "DELETE FROM quant.intraday_board_flow_snapshots WHERE observed_at<%s",
                    (cutoff,),
                )
                # Rotation delivery receipts cascade with their event.  Raw
                # snapshots, daily bars and research evidence are unrelated.
                connection.execute(
                    "DELETE FROM quant.intraday_board_rotation_events WHERE last_observed_at<%s",
                    (rotation_cutoff,),
                )

        await dependencies.run_database(prune)

    await dependencies.run_loop(
        board_session=dependencies.board_session,
        prune_before=prune_before,
        storage_allowed=dependencies.storage_allowed,
        capture=dependencies.capture,
        curve_retention_days=dependencies.curve_retention_days,
        rotation_retention_days=dependencies.rotation_retention_days,
    )


__all__ = ["IntradayBoardCurveRuntimeDependencies", "run_intraday_board_curve_runtime_loop"]
