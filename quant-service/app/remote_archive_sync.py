"""Text-only incremental synchronization for the remote analyst archive."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import httpx
from fastapi import HTTPException

from .remote_archive_transport import RemoteArchiveTransport


class RemoteArchiveSyncService:
    """Coordinate bounded message/report deltas without media or backfill."""

    def __init__(
        self,
        *,
        settings: Callable[[], dict[str, Any]],
        transport: RemoteArchiveTransport,
        database: Any,
        run_database_blocking: Callable[..., Awaitable[Any]],
        message_cursor_state: Callable[[], dict[str, Any]],
        report_cursor_state: Callable[[str], dict[str, Any]],
        import_message: Callable[..., Any],
        import_report: Callable[..., Any],
        update_global_cursor: Callable[[Any], Any],
        update_report_cursor: Callable[[Any], Any],
        message_cursor_update: Callable[..., Any],
        report_cursor_update: Callable[..., Any],
        parse_timestamp: Callable[[Any], datetime | None],
        record_attempt: Callable[..., Any] | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._database = database
        self._run_database_blocking = run_database_blocking
        self._message_cursor_state = message_cursor_state
        self._report_cursor_state = report_cursor_state
        self._import_message = import_message
        self._import_report = import_report
        self._update_global_cursor = update_global_cursor
        self._update_report_cursor = update_report_cursor
        self._message_cursor_update = message_cursor_update
        self._report_cursor_update = report_cursor_update
        self._parse_timestamp = parse_timestamp
        self._record_attempt = record_attempt
        self._sleep = sleep
        self._lock = asyncio.Lock()
        # Report catalogs can take materially longer than one message delta.
        # Keep duplicate calls for one stream serialized, but never let a
        # report retry prevent a separately scheduled message sync from
        # acquiring the shared HTTP pacing gate.
        self._stream_locks = {"reports": asyncio.Lock(), "messages": asyncio.Lock()}
        self._last_started: dict[str, float] = {}

    async def _record(self, stream: str, status: str, started_at: datetime,
                      completed_at: datetime, error_code: str | None,
                      summary: dict[str, Any] | None = None) -> None:
        if self._record_attempt is None:
            return
        await self._run_database_blocking(
            self._record_attempt, self._database, stream, status, started_at,
            completed_at, error_code, summary or {}, timeout_seconds=20,
        )

    async def _get(self, client: httpx.AsyncClient, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._transport.get(
            client, path, params=params, settings=self._settings(), sleep=self._sleep,
        )

    async def _messages(self, client: httpx.AsyncClient, maximum: int) -> dict[str, Any]:
        cursor_state = await self._run_database_blocking(self._message_cursor_state, timeout_seconds=15)
        params: dict[str, Any] = {"limit": maximum}
        if cursor_state.get("remote_cursor"):
            params["cursor"] = str(cursor_state["remote_cursor"])
        elif cursor_state.get("received_after"):
            value = cursor_state["received_after"]
            params["received_after"] = value.isoformat() if isinstance(value, datetime) else str(value)
        else:
            # Initial sync is deliberately bounded to the recent day. There is
            # no implicit historical archive download.
            params["received_after"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        envelope = await self._get(client, "/messages/updates", params=params)
        summaries = envelope.get("items") or []
        if not isinstance(summaries, list) or not all(isinstance(item, dict) for item in summaries):
            raise HTTPException(status_code=502, detail="remote analyst message update page has an invalid items shape")
        summaries = summaries[:maximum]
        imported: list[dict[str, Any]] = []
        for summary in summaries:
            analyst_id = str(summary.get("analyst_id") or "").strip()
            message_id = str(summary.get("message_id") or "").strip()
            if not analyst_id or not message_id:
                raise HTTPException(status_code=502, detail="remote analyst message update is missing analyst_id or message_id")
            detail = await self._get(client, f"/analysts/{analyst_id}/messages/{message_id}")
            imported.append(await self._run_database_blocking(self._import_message, self._database, detail, timeout_seconds=30))
        if summaries:
            received_after = self._parse_timestamp(summaries[-1].get("received_at"))
            if received_after is None:
                raise HTTPException(status_code=502, detail="remote analyst message update is missing received_at")
            next_cursor = envelope.get("next_cursor")
            await self._run_database_blocking(
                self._update_global_cursor,
                self._message_cursor_update(
                    stream_key="message_updates", cursor=str(next_cursor) if next_cursor else None,
                    received_after=received_after, terminal=next_cursor is None,
                    message_ids=[str(item.get("message_id")) for item in summaries if item.get("message_id")],
                ), timeout_seconds=20,
            )
        return {"status": "completed", "items": len(summaries), "imported": len(imported),
                "terminal": envelope.get("next_cursor") is None, "source": "remote_text_messages"}

    async def _reports(self, client: httpx.AsyncClient, maximum: int) -> dict[str, Any]:
        catalog = await self._get(client, "/analysts")
        analysts = catalog.get("items") or []
        if not isinstance(analysts, list) or not all(isinstance(item, dict) for item in analysts):
            raise HTTPException(status_code=502, detail="remote analyst catalog has an invalid items shape")
        imported = changed = scanned = 0
        remaining = maximum
        for analyst in analysts:
            if remaining <= 0:
                break
            analyst_id = str(analyst.get("analyst_id") or "").strip()
            if not analyst_id:
                continue
            cursor_state = await self._run_database_blocking(self._report_cursor_state, analyst_id, timeout_seconds=15)
            known_versions = dict(cursor_state.get("report_versions") or {})
            reports = (await self._get(
                client, f"/analysts/{analyst_id}/reports", params={"limit": min(100, remaining), "offset": 0}
            )).get("items") or []
            if not isinstance(reports, list) or not all(isinstance(item, dict) for item in reports):
                raise HTTPException(status_code=502, detail="remote analyst report page has an invalid items shape")
            versions: dict[str, str] = {}
            page = reports[:remaining]
            for report in page:
                report_date = str(report.get("date") or "")
                stamp = f"{report.get('version') or ''}:{report.get('content_hash') or ''}"
                if not report_date or stamp == ":":
                    continue
                versions[report_date] = stamp
                scanned += 1
                if known_versions.get(report_date) == stamp:
                    continue
                detail = await self._get(client, f"/analysts/{analyst_id}/reports/{report_date}")
                await self._run_database_blocking(self._import_report, self._database, detail, timeout_seconds=30)
                imported += 1
                changed += 1
            if versions:
                await self._run_database_blocking(
                    self._update_report_cursor,
                    self._report_cursor_update(stream_key="reports", analyst_id=analyst_id, report_versions=versions),
                    timeout_seconds=20,
                )
            remaining -= len(page)
        return {"status": "completed", "analysts": len(analysts), "scanned": scanned,
                "changed": changed, "imported": imported, "source": "remote_text_reports"}

    async def sync(self, payload: Any, authorization: str | None = None) -> dict[str, Any]:
        settings = self._settings()
        bearer = str(authorization or "").strip()
        if bearer.lower().startswith("bearer "):
            bearer = bearer[7:].strip()
        if not settings["base_url"] or not bearer:
            raise HTTPException(status_code=503, detail="remote analyst archive sync is not configured")
        maximum = min(payload.max_items, int(settings["max_items"]))
        async with self._lock:
            now = asyncio.get_running_loop().time()
            minimum_interval = float(settings["minimum_interval_seconds"])
            for stream in payload.streams:
                elapsed = now - self._last_started.get(stream, 0.0)
                if self._last_started.get(stream) and elapsed < minimum_interval:
                    raise HTTPException(status_code=429, detail=f"remote analyst archive {stream} sync is rate limited locally")
            for stream in payload.streams:
                self._last_started[stream] = now
        transport: dict[str, Any] = {
            "timeout": httpx.Timeout(30.0), "trust_env": False,
            "headers": {"Authorization": f"Bearer {bearer}"},
            "limits": httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=20.0),
        }
        if settings["ca_file"]:
            transport["verify"] = settings["ca_file"]
        async with httpx.AsyncClient(base_url=str(settings["base_url"]), **transport) as client:
            results: dict[str, Any] = {}
            for stream in payload.streams:
                # A one-stream n8n workflow owns this lock for only its own
                # cursor mutation.  RemoteArchiveTransport still serializes
                # individual requests and respects upstream Retry-After.
                started_at = datetime.now(timezone.utc)
                try:
                    async with self._stream_locks[stream]:
                        result = await (
                            self._messages(client, maximum) if stream == "messages" else self._reports(client, maximum)
                        )
                except Exception as error:
                    error_code = (
                        f"http_{error.status_code}" if isinstance(error, HTTPException) else type(error).__name__
                    )
                    await self._record(stream, "failed", started_at, datetime.now(timezone.utc), error_code)
                    raise
                await self._record(stream, "completed", started_at, datetime.now(timezone.utc), None, result)
                results[stream] = result
        return {"status": "completed", "streams": results, "text_only": True, "history_fetch": False}


__all__ = ["RemoteArchiveSyncService"]
