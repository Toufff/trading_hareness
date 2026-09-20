"""Read-only endpoints an external consumer needs to stay correct.

Two halves of one problem.  ``/api/v1/peer/contract`` answers "what is actually
here", so a consumer never has to infer a schema from prose again.
``/api/v1/peer/errors`` answers "what did I break", because a failure the
consumer swallows is recorded only in the owner's log and is otherwise
invisible to the one party able to fix it.

Both are guarded by the shared read key the peer deployment already carries.
The error feed additionally restricts the role it will report on: the key is
not a licence to read the owner's own statements, which may embed data.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query

from ..peer_contract import build_contract
from ..peer_error_feed import LOG_TIMEZONE, collect_role_errors

#: A feed is for noticing a problem, not for archaeology.  A consumer wanting
#: more history should poll more often rather than ask for a year at once.
MAX_WINDOW_DAYS = 31

DEFAULT_WINDOW_HOURS = 24


def _parse_moment(raw: str | None, *, default: datetime, field: str) -> datetime:
    """Accept ISO 8601, treating a naive value as cluster-local time."""
    if raw is None or not raw.strip():
        return default
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=LOG_TIMEZONE)
    return parsed


def build_peer_support_router(
    database: Any,
    *,
    shared_read_key: Callable[[], str],
    log_directory: Callable[[], str],
    allowed_roles: Callable[[], tuple[str, ...]],
    contract_builder: Callable[..., dict[str, Any]] = build_contract,
    now: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter(tags=["peer-support"])
    clock = now or (lambda: datetime.now(tz=LOG_TIMEZONE))

    def authorize(supplied: str | None) -> None:
        expected = shared_read_key().strip()
        if not expected:
            raise HTTPException(status_code=503, detail="shared read gateway is disabled")
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="valid X-Quant-Read-Key is required")

    @router.get("/api/v1/peer/contract")
    def peer_contract(
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
        role: str | None = Query(default=None, description="role whose grants to report; must be allowlisted"),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        roles = allowed_roles()
        if not roles:
            raise HTTPException(status_code=503, detail="no consumer role is configured")
        target = (role or roles[0]).strip()
        if target not in roles:
            raise HTTPException(status_code=403, detail=f"role {target!r} is not published through this endpoint")
        with database.transaction() as connection:
            return contract_builder(connection, peer_role=target)

    @router.get("/api/v1/peer/errors")
    def peer_errors(
        x_quant_read_key: str | None = Header(default=None, alias="X-Quant-Read-Key"),
        role: str | None = Query(default=None, description="role to report on; must be allowlisted"),
        since: str | None = Query(default=None, description="ISO 8601; defaults to 24 hours ago"),
        until: str | None = Query(default=None, description="ISO 8601; defaults to now"),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        authorize(x_quant_read_key)
        roles = allowed_roles()
        if not roles:
            raise HTTPException(status_code=503, detail="no consumer role is configured")
        target = (role or roles[0]).strip()
        if target not in roles:
            raise HTTPException(status_code=403, detail=f"role {target!r} is not published through this endpoint")

        moment = clock()
        window_until = _parse_moment(until, default=moment, field="until")
        window_since = _parse_moment(since, default=moment - timedelta(hours=DEFAULT_WINDOW_HOURS), field="since")
        if window_since > window_until:
            raise HTTPException(status_code=422, detail="since must not be later than until")
        if window_until - window_since > timedelta(days=MAX_WINDOW_DAYS):
            raise HTTPException(status_code=422, detail=f"the window may not exceed {MAX_WINDOW_DAYS} days")

        directory = Path(log_directory())
        if not directory.is_dir():
            raise HTTPException(status_code=503, detail="the owner log directory is not readable from this service")
        payload = collect_role_errors(
            log_dir=directory,
            role=target,
            since=window_since,
            until=window_until,
            limit=limit,
        )
        payload["live_effect"] = "none"
        return payload

    return router


__all__ = ["DEFAULT_WINDOW_HOURS", "MAX_WINDOW_DAYS", "build_peer_support_router"]
