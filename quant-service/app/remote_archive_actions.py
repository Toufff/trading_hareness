"""Write-side service boundary for the remote analyst text archive.

The HTTP router passes Pydantic payloads here, while this module owns the
archive cursors, local-only transport settings and process-scoped sync
coordinator.  It intentionally accepts only already-extracted text payloads;
it never follows a remote media URL.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from psycopg.types.json import Json

from .remote_archive import (
    analyst_global_sync_cursor,
    analyst_sync_cursor,
    import_remote_analyst_message,
    import_remote_report,
    parse_optional_timestamp,
    record_analyst_sync_attempt,
    reprocess_remote_messages,
    reprocess_remote_reports,
)
from .remote_archive_sync import RemoteArchiveSyncService
from .remote_archive_transport import RemoteArchiveTransport


class RemoteArchiveActions:
    """Small application service used by the remote-archive action routes."""

    def __init__(
        self,
        *,
        database: Any,
        run_database_blocking: Callable[..., Awaitable[Any]],
        message_cursor_update: Callable[..., Any],
        report_cursor_update: Callable[..., Any],
        transport: RemoteArchiveTransport | None = None,
    ) -> None:
        self._database = database
        self._run_database_blocking = run_database_blocking
        self._message_cursor_update = message_cursor_update
        self._report_cursor_update = report_cursor_update
        self._transport = transport or RemoteArchiveTransport()
        self._sync_service: RemoteArchiveSyncService | None = None

    @staticmethod
    def sync_settings() -> dict[str, Any]:
        """Read local-only settings without returning a bearer credential."""
        base_url = os.getenv("REMOTE_ANALYST_ARCHIVE_BASE_URL", "").strip().rstrip("/")
        ca_file = os.getenv("REMOTE_ANALYST_ARCHIVE_CA_FILE", "").strip()
        try:
            configured_max_items = int(os.getenv("REMOTE_ANALYST_SYNC_MAX_ITEMS", "100"))
        except ValueError:
            configured_max_items = 100
        try:
            minimum_interval_seconds = float(os.getenv("REMOTE_ANALYST_SYNC_MIN_INTERVAL_SECONDS", "15"))
        except ValueError:
            minimum_interval_seconds = 15.0
        try:
            request_interval_seconds = float(os.getenv("REMOTE_ANALYST_SYNC_REQUEST_INTERVAL_SECONDS", "2"))
        except ValueError:
            request_interval_seconds = 2.0
        return {
            "base_url": base_url,
            "ca_file": ca_file if ca_file and Path(ca_file).is_file() else None,
            "max_items": min(100, max(1, configured_max_items)),
            "minimum_interval_seconds": min(300.0, max(1.0, minimum_interval_seconds)),
            "request_interval_seconds": min(30.0, max(0.0, request_interval_seconds)),
        }

    def import_report(self, payload: Any) -> dict[str, Any]:
        try:
            return import_remote_report(self._database, payload.report)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def import_message(self, payload: Any) -> dict[str, Any]:
        try:
            return import_remote_analyst_message(self._database, payload.message)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def reprocess_reports(self, payload: Any) -> dict[str, Any]:
        return reprocess_remote_reports(self._database, payload.limit)

    def reprocess_messages(self, payload: Any) -> dict[str, Any]:
        return reprocess_remote_messages(self._database, payload.limit)

    def message_cursor_state(self) -> dict[str, Any]:
        return analyst_global_sync_cursor(self._database, "message_updates").get("cursor") or {}

    def report_cursor_state(self, analyst_id: str) -> dict[str, Any]:
        return analyst_sync_cursor(self._database, "reports", analyst_id).get("cursor") or {}

    def update_cursor(self, payload: Any) -> dict[str, Any]:
        """Persist a per-analyst report/message watermark after local import."""
        with self._database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM quant.remote_analysts WHERE remote_analyst_id=%s", (payload.analyst_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail="remote analyst not found")
            current = connection.execute(
                """SELECT received_at,message_ids,report_versions FROM quant.analyst_sync_cursors
                     WHERE stream_key=%s AND remote_analyst_id=%s FOR UPDATE""",
                (payload.stream_key, payload.analyst_id),
            ).fetchone()
            received_at = payload.received_at
            message_ids = list(dict.fromkeys(str(value) for value in payload.message_ids if str(value)))
            report_versions = {
                str(key): str(value) for key, value in payload.report_versions.items() if str(key) and str(value)
            }
            if current and payload.stream_key == "messages":
                previous_at = current["received_at"]
                if previous_at is not None and received_at is not None and received_at < previous_at:
                    received_at, message_ids = previous_at, list(current["message_ids"] or [])
                elif previous_at is not None and received_at == previous_at:
                    message_ids = list(dict.fromkeys([*(current["message_ids"] or []), *message_ids]))[:500]
            if current and payload.stream_key == "reports":
                report_versions = {**dict(current["report_versions"] or {}), **report_versions}
                if len(report_versions) > 500:
                    report_versions = dict(sorted(report_versions.items())[-500:])
            connection.execute(
                """INSERT INTO quant.analyst_sync_cursors(stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at)
                   VALUES(%s,%s,%s,%s,%s,now())
                   ON CONFLICT(stream_key,remote_analyst_id) DO UPDATE SET received_at=EXCLUDED.received_at,
                     message_ids=EXCLUDED.message_ids,report_versions=EXCLUDED.report_versions,updated_at=now()""",
                (payload.stream_key, payload.analyst_id, received_at, Json(message_ids), Json(report_versions)),
            )
        return {
            "status": "updated", "stream_key": payload.stream_key, "analyst_id": payload.analyst_id,
            "received_at": received_at.isoformat() if received_at else None,
            "message_ids": len(message_ids), "report_versions": len(report_versions),
        }

    def update_global_cursor(self, payload: Any) -> dict[str, Any]:
        """Commit a message change-feed page only after every item imported."""
        with self._database.transaction() as connection:
            current = connection.execute(
                """SELECT remote_cursor,received_after FROM quant.analyst_global_sync_cursors
                     WHERE stream_key=%s FOR UPDATE""",
                (payload.stream_key,),
            ).fetchone()
            remote_cursor = None if payload.terminal else (payload.cursor or (current["remote_cursor"] if current else None))
            received_after = payload.received_after or (current["received_after"] if current else None)
            connection.execute(
                """INSERT INTO quant.analyst_global_sync_cursors(stream_key,remote_cursor,received_after,updated_at)
                   VALUES(%s,%s,%s,now())
                   ON CONFLICT(stream_key) DO UPDATE SET remote_cursor=EXCLUDED.remote_cursor,
                     received_after=EXCLUDED.received_after,updated_at=now()""",
                (payload.stream_key, remote_cursor, received_after),
            )
        return {
            "status": "updated", "stream_key": payload.stream_key,
            "has_cursor": bool(remote_cursor),
            "received_after": received_after.isoformat() if received_after else None,
        }

    async def sync(self, payload: Any, authorization: str | None = None) -> dict[str, Any]:
        if self._sync_service is None:
            self._sync_service = RemoteArchiveSyncService(
                settings=self.sync_settings, transport=self._transport, database=self._database,
                run_database_blocking=self._run_database_blocking,
                message_cursor_state=self.message_cursor_state, report_cursor_state=self.report_cursor_state,
                import_message=import_remote_analyst_message, import_report=import_remote_report,
                update_global_cursor=self.update_global_cursor, update_report_cursor=self.update_cursor,
                message_cursor_update=self._message_cursor_update, report_cursor_update=self._report_cursor_update,
                parse_timestamp=parse_optional_timestamp, record_attempt=record_analyst_sync_attempt, sleep=asyncio.sleep,
            )
        return await self._sync_service.sync(payload, authorization)


__all__ = ["RemoteArchiveActions"]
