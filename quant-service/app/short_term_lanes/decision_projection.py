"""Project terminal decision dossiers back onto the scan that selected them.

This is deliberately a read projection.  The executor remains in
``decision_research_service``; reports never manufacture a research result.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..decision_research_repository import latest_dossiers


TERMINAL = {"passed", "rejected"}


def load(database: Any, day: date) -> list[dict[str, Any]]:
    with database.transaction() as connection:
        return latest_dossiers(connection, day, 100)


def project(review_plan: list[dict[str, Any]], dossiers: list[dict[str, Any]]) -> dict[str, Any]:
    planned = [str(item["symbol"]) for item in review_plan]
    by_symbol: dict[str, dict[str, Any]] = {}
    for dossier in dossiers:
        evidence = dossier.get("evidence_snapshot") or {}
        if evidence.get("role") != "candidate" or dossier.get("symbol") not in planned:
            continue
        current = by_symbol.get(str(dossier["symbol"]))
        if current is None or str(dossier.get("created_at") or "") > str(current.get("created_at") or ""):
            by_symbol[str(dossier["symbol"])] = dossier
    ordered = [by_symbol[symbol] for symbol in planned if symbol in by_symbol]
    terminal = [row for row in ordered if row.get("status") in TERMINAL]
    incomplete = [row for row in ordered if row.get("status") == "incomplete"]
    missing = [symbol for symbol in planned if symbol not in by_symbol]
    return {
        "version": "lane-decision-research-projection-v1",
        "status": (
            "complete" if len(terminal) == len(planned)
            else "terminal_with_gaps" if len(ordered) == len(planned)
            else "not_run"
        ),
        "planned": len(planned),
        "terminal": len(terminal),
        "incomplete": len(incomplete),
        "missing_symbols": missing,
        "dossiers": ordered,
        "boundary": "terminal decision gates; separate from manual primary-filing company review and broker holdings",
    }


__all__ = ["load", "project"]
