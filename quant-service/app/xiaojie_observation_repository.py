"""Persistence and new-candidate detection for xiaojie leader-flow observations.

A candidate that keeps its setup for an hour is one observation with a widening
window, not one row per 30-second scan.  Collapsing on (session, symbol, mode)
keeps a day's research readable and, more usefully, makes "this is new" a
property of the insert itself: only a row that did not exist before is a fresh
signal, and only a fresh signal is worth interrupting a human for.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from psycopg.types.json import Json

from .xiaojie_leader_flow import MODEL_VERSION


def record_candidates(connection: Any, trading_date: date, observed_at: datetime,
                      scan_id: Any, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upsert this scan's candidates and return only the newly appearing ones.

    ``xmax = 0`` is PostgreSQL's marker for a tuple this statement inserted
    rather than updated, so the "is this new" question is answered by the write
    itself instead of a separate read that another scan could race.
    """
    fresh: list[dict[str, Any]] = []
    for candidate in candidates:
        mode = str(candidate.get("mode") or "unclassified")
        row = connection.execute(
            """INSERT INTO quant.xiaojie_leader_flow_observations(
                    trading_date,symbol,mode,model_version,first_seen_at,last_seen_at,
                    observation_count,first_scan_id,decision,target_fraction,stop_loss,
                    exit_state,risk_flags,reasons,market_gate,first_evidence,last_evidence)
               SELECT %s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(trading_date,symbol,mode) DO UPDATE SET
                 last_seen_at=EXCLUDED.last_seen_at,
                 observation_count=quant.xiaojie_leader_flow_observations.observation_count+1,
                 decision=EXCLUDED.decision,target_fraction=EXCLUDED.target_fraction,
                 exit_state=EXCLUDED.exit_state,risk_flags=EXCLUDED.risk_flags,
                 last_evidence=EXCLUDED.last_evidence
               RETURNING symbol, mode, (xmax = 0) AS inserted""",
            (trading_date, candidate["symbol"], mode, MODEL_VERSION, observed_at, observed_at,
             scan_id, candidate.get("decision"),
             (candidate.get("position") or {}).get("target_fraction"),
             Json(candidate.get("stop_loss") or {}), Json(candidate.get("exit") or {}),
             Json(candidate.get("risk_flags") or []), Json(candidate.get("reasons") or []),
             Json(candidate.get("market_gate") or {}), Json(candidate.get("evidence") or {}),
             Json(candidate.get("evidence") or {}), candidate["symbol"]),
        ).fetchone()
        if row is not None and row["inserted"]:
            fresh.append(candidate)
    return fresh


def mark_alerted(connection: Any, trading_date: date, alerted_at: datetime,
                 entries: list[tuple[str, str]]) -> int:
    """Stamp the observations an alert actually went out for."""
    for symbol, mode in entries:
        connection.execute(
            """UPDATE quant.xiaojie_leader_flow_observations SET alerted_at=%s
                WHERE trading_date=%s AND symbol=%s AND mode=%s AND alerted_at IS NULL""",
            (alerted_at, trading_date, symbol, mode),
        )
    return len(entries)


def session_observations(connection: Any, trading_date: date) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT symbol,mode,decision,target_fraction,first_seen_at,last_seen_at,
                  observation_count,risk_flags,reasons,last_evidence,alerted_at
             FROM quant.xiaojie_leader_flow_observations
            WHERE trading_date=%s ORDER BY first_seen_at, symbol""",
        (trading_date,),
    ).fetchall()
    return [dict(row) for row in rows]


__all__ = ["mark_alerted", "record_candidates", "session_observations"]
