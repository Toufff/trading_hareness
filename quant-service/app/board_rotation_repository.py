"""Persisted state machine for same-source board-flow rotation evidence."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


class BoardRotationRepository:
    """Advance a board rotation using adjacent persisted snapshots only."""

    def __init__(self, database: Any) -> None:
        self._database = database

    def evaluate(
        self,
        snapshot_minute: datetime,
        observed_at: datetime,
        *,
        candidates_for: Callable[[list[dict[str, Any]], list[dict[str, Any]]], list[dict[str, Any]]],
        still_directional: Callable[[dict[str, Any], list[dict[str, Any]]], bool],
    ) -> list[dict[str, Any]]:
        """Advance confirmed rotations and create bounded next-minute candidates.

        All inputs are local stored Eastmoney snapshots.  The method never
        makes a provider call or merges taxonomies by display label.
        """
        exchange_start = datetime.combine(
            snapshot_minute.astimezone(ZoneInfo("Asia/Shanghai")).date(), time(9, 20), tzinfo=ZoneInfo("Asia/Shanghai"),
        ).astimezone(timezone.utc)
        with self._database.transaction() as connection:
            current_row = connection.execute(
                """SELECT snapshot_minute,payload FROM quant.intraday_board_flow_snapshots
                     WHERE snapshot_minute=%s AND status IN ('completed','partial')""",
                (snapshot_minute,),
            ).fetchone()
            previous_row = connection.execute(
                """SELECT snapshot_minute,payload FROM quant.intraday_board_flow_snapshots
                     WHERE snapshot_minute<%s AND snapshot_minute>=%s AND status IN ('completed','partial')
                     ORDER BY snapshot_minute DESC LIMIT 1""",
                (snapshot_minute, exchange_start),
            ).fetchone()
            if (current_row is None or previous_row is None
                    or snapshot_minute - previous_row["snapshot_minute"] > timedelta(minutes=2)):
                return []
            current_items = list((current_row["payload"] or {}).get("items") or [])
            previous_items = list((previous_row["payload"] or {}).get("items") or [])
            connection.execute(
                """UPDATE quant.intraday_board_rotation_events
                      SET state='expired',updated_at=now()
                    WHERE state='confirming' AND confirmation_deadline<%s""",
                (observed_at,),
            )
            pending_rows = connection.execute(
                """SELECT * FROM quant.intraday_board_rotation_events
                     WHERE state='confirming' AND confirmation_deadline>=%s
                     ORDER BY first_observed_at""",
                (observed_at - timedelta(minutes=2),),
            ).fetchall()
            confirmed: list[dict[str, Any]] = []
            active_keys: set[str] = set()
            for row in pending_rows:
                event = dict(row)
                active_keys.add(str(event["event_key"]))
                if event["snapshot_minute"] >= snapshot_minute:
                    continue
                conditions = dict(event.get("conditions") or {})
                if not still_directional({**event, **conditions}, current_items):
                    continue
                updated = connection.execute(
                    """UPDATE quant.intraday_board_rotation_events
                          SET state='confirmed',snapshot_minute=%s,last_observed_at=%s,
                              updated_at=now()
                        WHERE rotation_event_id=%s
                        RETURNING *""",
                    (snapshot_minute, observed_at, event["rotation_event_id"]),
                ).fetchone()
                confirmed.append(dict(updated))
            for candidate in candidates_for(previous_items, current_items):
                if candidate["event_key"] in active_keys:
                    continue
                recent_event = connection.execute(
                    """SELECT state FROM quant.intraday_board_rotation_events
                         WHERE event_key=%s AND state IN ('confirming','confirmed','alerted','suppressed')
                           AND last_observed_at>=%s
                         LIMIT 1""",
                    (candidate["event_key"], observed_at - timedelta(minutes=10)),
                ).fetchone()
                if recent_event is not None:
                    continue
                connection.execute(
                    """INSERT INTO quant.intraday_board_rotation_events(
                           snapshot_minute,event_key,taxonomy_key,sector_key,label,event_type,direction,state,
                           first_observed_at,last_observed_at,confirmation_deadline,conditions
                       ) VALUES(%s,%s,%s,%s,%s,%s,%s,'confirming',%s,%s,%s,%s)""",
                    (snapshot_minute, candidate["event_key"], candidate["taxonomy_key"], candidate["sector_key"],
                     candidate["label"], candidate["event_type"], candidate["direction"],
                     observed_at, observed_at, observed_at + timedelta(minutes=2), Json(candidate)),
                )
        return confirmed
