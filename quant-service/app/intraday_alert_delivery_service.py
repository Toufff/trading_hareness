"""Durable intraday-alert outbox operations.

This module owns the Feishu delivery state transitions only.  Market-signal
selection remains in the monitor, and the application supplies its executor
and transport so this layer has no FastAPI or HTTP-client lifecycle coupling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
import uuid

from psycopg.types.json import Json


DatabaseExecutor = Callable[[Callable[[], Any]], Awaitable[Any]]
AlertSender = Callable[[str], Awaitable[dict[str, Any]]]
JsonSafe = Callable[[Any], Any]
RecoveryText = Callable[[int], str]


def create_pending_delivery(database: Any, signal_event_id: uuid.UUID, text: str) -> uuid.UUID:
    """Write the outbox row before any outbound request is attempted."""
    with database.transaction() as connection:
        row = connection.execute(
            """INSERT INTO quant.intraday_alert_deliveries(
                   signal_event_id,channel,status,message_text,next_attempt_at
               ) VALUES(%s,'feishu_adapter','pending',%s,now()) RETURNING delivery_id""",
            (signal_event_id, text),
        ).fetchone()
    return row["delivery_id"]


async def attempt_delivery(
    database: Any,
    delivery_id: uuid.UUID,
    signal_event_id: uuid.UUID,
    text: str,
    *,
    post_text: AlertSender,
    run_database: DatabaseExecutor,
    json_safe: JsonSafe,
    recovery_text: RecoveryText,
    max_attempts: int,
) -> dict[str, Any]:
    """Send one outbox row, preserving both failures and recovery receipts."""
    outcome = await post_text(text)

    def persist_delivery_attempt() -> dict[str, Any] | None:
        with database.transaction() as connection:
            # Ignore the row currently being attempted.  Board reports and
            # daily summaries share the same Feishu channel health meaning.
            history_rows = connection.execute(
                """SELECT status FROM (
                       SELECT status,created_at FROM quant.intraday_alert_deliveries
                        WHERE delivery_id<>%s
                       UNION ALL
                       SELECT status,created_at FROM quant.intraday_board_report_deliveries
                       UNION ALL
                       SELECT delivery_status AS status,updated_at AS created_at FROM quant.strategy_day_summaries
                   ) delivery ORDER BY created_at DESC LIMIT 20""",
                (delivery_id,),
            ).fetchall()
            prior_failed_streak = 0
            for row in history_rows:
                status = str(row["status"])
                if status == "pending":
                    continue
                if status == "failed":
                    prior_failed_streak += 1
                    continue
                break
            connection.execute(
                """UPDATE quant.intraday_alert_deliveries
                      SET status=%s,response=%s,error_message=%s,
                          sent_at=%s,attempt_count=attempt_count+1,
                          next_attempt_at=CASE WHEN %s='failed' AND attempt_count+1<%s
                                               THEN now()+interval '30 seconds' ELSE NULL END
                    WHERE delivery_id=%s""",
                (outcome["status"], Json(json_safe(outcome.get("response", {}))), outcome.get("error") or outcome.get("reason"),
                 datetime.now(timezone.utc) if outcome["status"] == "sent" else None,
                 outcome["status"], max_attempts, delivery_id),
            )
            if outcome["status"] == "sent":
                connection.execute("UPDATE quant.intraday_signal_events SET state='alerted' WHERE signal_event_id=%s", (signal_event_id,))
            if outcome["status"] == "failed" and prior_failed_streak + 1 == 3:
                event = connection.execute(
                    """INSERT INTO quant.alert_delivery_health_events(
                           channel,event_type,source_reference,streak_count,delivery_status,message_text
                       ) VALUES('feishu_adapter','failure_streak',%s,%s,'observed',%s)
                       ON CONFLICT(channel,event_type,source_reference) DO NOTHING
                       RETURNING health_event_id,event_type,streak_count,message_text""",
                    (str(delivery_id), prior_failed_streak + 1,
                     f"Feishu alert delivery has failed {prior_failed_streak + 1} consecutive times"),
                ).fetchone()
                return dict(event) if event else None
            if outcome["status"] == "sent" and prior_failed_streak >= 3:
                event = connection.execute(
                    """INSERT INTO quant.alert_delivery_health_events(
                           channel,event_type,source_reference,streak_count,delivery_status,message_text
                       ) VALUES('feishu_adapter','recovered',%s,%s,'pending',%s)
                       ON CONFLICT(channel,event_type,source_reference) DO NOTHING
                       RETURNING health_event_id,event_type,streak_count,message_text""",
                    (str(delivery_id), prior_failed_streak, recovery_text(prior_failed_streak)),
                ).fetchone()
                return dict(event) if event else None
        return None

    health_event = await run_database(persist_delivery_attempt)
    if health_event and health_event["event_type"] == "recovered":
        health_outcome = await post_text(str(health_event["message_text"]))

        def persist_health_event_attempt() -> None:
            with database.transaction() as connection:
                connection.execute(
                    """UPDATE quant.alert_delivery_health_events
                          SET delivery_status=%s,response=%s,error_message=%s,sent_at=%s,updated_at=now()
                        WHERE health_event_id=%s""",
                    (health_outcome["status"], Json(json_safe(health_outcome.get("response", {}))),
                     health_outcome.get("error") or health_outcome.get("reason"),
                     datetime.now(timezone.utc) if health_outcome["status"] == "sent" else None,
                     health_event["health_event_id"]),
                )
        await run_database(persist_health_event_attempt)
    return outcome


def load_due_deliveries(database: Any, max_attempts: int, limit: int) -> list[dict[str, Any]]:
    """Return a bounded set of unsent due rows, never re-sending an event."""
    with database.transaction() as connection:
        rows = connection.execute(
            """SELECT d.delivery_id,d.signal_event_id,d.message_text
                 FROM quant.intraday_alert_deliveries d
                WHERE d.channel='feishu_adapter' AND d.status IN ('pending','failed')
                  AND d.message_text IS NOT NULL AND d.message_text<>''
                  AND d.attempt_count<%s
                  AND coalesce(d.next_attempt_at,d.created_at)<=now()
                  AND NOT EXISTS (
                      SELECT 1 FROM quant.intraday_alert_deliveries sent
                       WHERE sent.signal_event_id=d.signal_event_id AND sent.status='sent'
                  )
                ORDER BY d.created_at LIMIT %s""",
            (max_attempts, max(1, min(limit, 10))),
        ).fetchall()
    return [dict(row) for row in rows]
