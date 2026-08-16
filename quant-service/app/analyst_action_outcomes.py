"""Replay-only, author-stated intraday outcomes for Anqiang actions.

This is intentionally not an analyst-factor or live-alert input.  It answers
the retrospective question "what did the locally recorded market do after the
author-stated action time?" while retaining `received_at` as the only clock
available to a live strategy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Json

from .analyst_intraday_settlement import (
    EXIT_QUOTE_TOLERANCE_SECONDS,
    entry_quote_window,
    has_bounded_query_window,
    json_safe_window,
)
from .intraday_clock import intraday_outcome_window


ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION = "author-stated-local-quote-session-bounded-replay-v1"
ANQIANG_ACTION_REPLAY_HORIZONS = (5, 15, 30, 60)


def materialize_anqiang_action_replay_outcomes(connection: Any, *, cutoff_at: datetime | None = None,
                                                limit: int = 500) -> dict[str, Any]:
    """Settle author-stated actions solely for replay, using local Tencent rows.

    No provider call occurs here.  An unavailable author-stated timestamp or
    missing local quote stays explicit rather than being replaced with a daily
    close or a later-session quote.
    """
    cutoff_at = cutoff_at or datetime.now(timezone.utc)
    rows = connection.execute(
        """SELECT action_id,symbol,direction,stated_at,available_at
             FROM quant.analyst_trade_actions
            WHERE remote_analyst_id='anqiang-touzi-riji' AND direction<>0
              AND stated_at<=%s
            ORDER BY stated_at,action_id LIMIT %s""",
        (cutoff_at, max(1, min(limit, 2000))),
    ).fetchall()
    counts = {"matured": 0, "pending": 0, "unavailable": 0}
    for raw_row in rows:
        row = dict(raw_row)
        entry_window = entry_quote_window(row["stated_at"], cutoff_at=cutoff_at)
        entry = None
        if has_bounded_query_window(entry_window):
            entry = connection.execute(
                """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                     WHERE symbol=%s AND source_name='tencent_free'
                       AND observed_at>=%s AND observed_at<=%s AND price>0
                     ORDER BY observed_at LIMIT 1""",
                (row["symbol"], entry_window["query_start"], entry_window["query_end"]),
            ).fetchone()
        for horizon in ANQIANG_ACTION_REPLAY_HORIZONS:
            status, exit_row = "pending", None
            settlement: dict[str, Any] = {
                "clock_basis": "author_stated_at",
                "replay_only": True,
                "strategy_effect": "none",
                "entry_window": json_safe_window(entry_window),
                "source": "tencent_free",
                "session_bounded": True,
            }
            if entry is None:
                status = str(entry_window["status"])
                settlement["reason"] = entry_window.get("reason")
            else:
                exit_window = intraday_outcome_window(
                    entry["observed_at"], horizon_minutes=horizon, cutoff=cutoff_at,
                    tolerance_seconds=EXIT_QUOTE_TOLERANCE_SECONDS,
                )
                settlement["entry_observed_at"] = entry["observed_at"].isoformat()
                settlement["exit_window"] = json_safe_window(exit_window)
                if has_bounded_query_window(exit_window):
                    exit_row = connection.execute(
                        """SELECT observed_at,price,source_name FROM quant.intraday_quote_observations
                             WHERE symbol=%s AND source_name=%s
                               AND observed_at>=%s AND observed_at<=%s AND price>0
                             ORDER BY observed_at LIMIT 1""",
                        (row["symbol"], entry["source_name"], exit_window["query_start"], exit_window["query_end"]),
                    ).fetchone()
                status = "matured" if exit_row is not None else str(exit_window["status"])
                settlement["reason"] = "first_quote_within_target_tolerance" if exit_row else exit_window.get("reason")
            directional_return = None
            if status == "matured" and entry is not None and exit_row is not None:
                directional_return = (Decimal(str(exit_row["price"])) / Decimal(str(entry["price"])) - 1) * int(row["direction"])
            connection.execute(
                """INSERT INTO quant.analyst_action_intraday_outcomes(
                     action_id,methodology_version,horizon_minutes,status,entry_at,entry_price,exit_at,exit_price,
                     directional_return,source_name,settlement,calculated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(action_id,methodology_version,horizon_minutes) DO UPDATE SET status=EXCLUDED.status,
                     entry_at=EXCLUDED.entry_at,entry_price=EXCLUDED.entry_price,exit_at=EXCLUDED.exit_at,
                     exit_price=EXCLUDED.exit_price,directional_return=EXCLUDED.directional_return,
                     source_name=EXCLUDED.source_name,settlement=EXCLUDED.settlement,calculated_at=now()""",
                (row["action_id"], ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION, horizon, status,
                 entry["observed_at"] if entry else None, entry["price"] if entry else None,
                 exit_row["observed_at"] if exit_row else None, exit_row["price"] if exit_row else None,
                 directional_return, (exit_row or entry or {}).get("source_name"), Json(settlement)),
            )
            counts[status] += 1
    return {
        "analyst_id": "anqiang-touzi-riji", "actions": len(rows), "outcomes": counts,
        "cutoff_at": cutoff_at, "methodology_version": ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION,
        "data_boundary": "author-stated-time retrospective replay only; no live strategy effect",
    }


__all__ = [
    "ANQIANG_ACTION_REPLAY_HORIZONS", "ANQIANG_ACTION_REPLAY_METHODOLOGY_VERSION",
    "materialize_anqiang_action_replay_outcomes",
]
