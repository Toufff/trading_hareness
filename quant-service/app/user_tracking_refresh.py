"""Refresh durable analysis snapshots for explicit user-tracked stocks."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

from psycopg.types.json import Json

from .user_tracking_research import build_snapshot as project_tracking_snapshot, has_active_user_tracking_tag


def persist_snapshot(database: Any, symbol: str, snapshot: dict[str, Any]) -> None:
    with database.transaction() as connection:
        connection.execute(
            """UPDATE quant.intraday_watchlists
                  SET metadata=jsonb_set(
                        metadata - 'user_tracking_research_error',
                        '{user_tracking_research}',%s::jsonb,true
                      ),updated_at=now()
                WHERE symbol=%s""",
            (Json(snapshot), symbol),
        )


def persist_error(database: Any, symbol: str, error: str) -> None:
    payload = {"at": datetime.now(timezone.utc).isoformat(), "error": error[:600]}
    with database.transaction() as connection:
        connection.execute(
            """UPDATE quant.intraday_watchlists
                  SET metadata=jsonb_set(metadata,'{user_tracking_research_error}',%s::jsonb,true),updated_at=now()
                WHERE symbol=%s""",
            (Json(payload), symbol),
        )


async def refresh(
    rows: list[dict[str, Any]],
    *,
    as_of_date: date | None,
    build_workbench: Callable[[str, date | None], Awaitable[dict[str, Any]]],
    save_snapshot: Callable[[str, dict[str, Any]], Awaitable[None]],
    save_error: Callable[[str, str], Awaitable[None]] | None = None,
    symbols: set[str] | None = None,
) -> dict[str, Any]:
    targets = [
        row for row in rows
        if has_active_user_tracking_tag(row.get("metadata"))
        and (symbols is None or str(row.get("symbol")) in symbols)
    ]
    results: list[dict[str, Any]] = []
    for row in targets:
        symbol = str(row["symbol"])
        try:
            workbench = await build_workbench(symbol, as_of_date)
            snapshot = project_tracking_snapshot(workbench)
            await save_snapshot(symbol, snapshot)
            results.append({
                "symbol": symbol, "name": snapshot.get("name"), "status": snapshot["status"],
                "stance": snapshot["stance"], "as_of_date": snapshot.get("as_of_date"),
            })
        except Exception as error:  # one stock must not erase other completed research
            detail = f"{type(error).__name__}: {error}"
            if save_error is not None:
                await save_error(symbol, detail)
            results.append({"symbol": symbol, "status": "failed", "error": detail[:600]})
    failed = [item for item in results if item["status"] == "failed"]
    return {
        "status": "completed" if not failed else "partial",
        "as_of_date": str(as_of_date) if as_of_date else None,
        "requested": len(targets), "completed": len(results) - len(failed), "failed": len(failed),
        "items": results, "depends_on_holdings": False, "buy_authorized": False,
    }


__all__ = ["persist_error", "persist_snapshot", "refresh"]
