"""Causal event adapter for locally mounted offline minute bars.

This module is deliberately narrow: it turns a persisted bar with a proven
vendor availability clock into a :class:`MarketEvent`.  It neither downloads
history nor fabricates the peer, quote and factor snapshots required to rerun
live entry rules.  Those missing inputs keep price-path replay blocked until a
complete, locally recorded evidence bundle is supplied.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from .intraday_replay import market_event_from_row, replay_order_key
from .strategy_contracts import MarketEvent


OFFLINE_MINUTE_REPLAY_SCHEMA_VERSION = "offline-minute-bar-v1"
OFFLINE_MINUTE_REPLAY_SOURCE = "offline_minute_bar"


def _utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"offline minute replay requires datetime {field}")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    """Keep the bar payload explicit and stable; raw vendor fields remain DB evidence."""
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "bar_time": _utc(row.get("bar_time"), field="bar_time").isoformat(),
        "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
        "close": row.get("close"), "volume": row.get("volume"), "amount": row.get("amount"),
        "source_name": str(row.get("source_name") or ""),
        "import_id": str(row.get("import_id") or ""),
    }


def offline_minute_market_event(row: dict[str, Any]) -> MarketEvent:
    """Create one replay envelope, rejecting an absent or impossible clock.

    ``available_at`` is the local persistence timestamp.  ``source_available_at``
    must come from the imported source and is the only allowed replay clock.
    It may be later than the bar close, but can never be later than local
    ingestion: otherwise this service could not have observed it then.
    """
    source_available_at = _utc(row.get("source_available_at"), field="source_available_at")
    local_available_at = _utc(row.get("available_at"), field="available_at")
    if source_available_at > local_available_at:
        raise ValueError("source_available_at cannot be after local available_at")
    payload = _payload(row)
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    event_row = {
        **row,
        "event_id": row.get("event_id") or hashlib.sha256(
            f"{OFFLINE_MINUTE_REPLAY_SOURCE}:{payload_hash}:{source_available_at.isoformat()}".encode()
        ).hexdigest(),
        "event_time": _utc(row.get("bar_time"), field="bar_time"),
        "available_at": source_available_at,
        "ingested_at": local_available_at,
        "quality_flags": tuple(row.get("quality_flags") or ()) + ("offline_source_availability_replay_only",),
    }
    return market_event_from_row(
        event_row,
        source=OFFLINE_MINUTE_REPLAY_SOURCE,
        schema_version=OFFLINE_MINUTE_REPLAY_SCHEMA_VERSION,
        payload=payload,
    )


def offline_minute_replay_events(rows: Iterable[dict[str, Any]]) -> list[tuple[MarketEvent, int, dict[str, Any]]]:
    """Stable, provider-free event list for generic replay primitives.

    The sequence is derived only after ordering by source availability, then
    the immutable event id.  This makes input file iteration order irrelevant.
    """
    events = [(offline_minute_market_event(row), _payload(row)) for row in rows]
    ordered = sorted(events, key=lambda item: replay_order_key(item[0]))
    return [(event, sequence, payload) for sequence, (event, payload) in enumerate(ordered, start=1)]


__all__ = [
    "OFFLINE_MINUTE_REPLAY_SCHEMA_VERSION", "OFFLINE_MINUTE_REPLAY_SOURCE",
    "offline_minute_market_event", "offline_minute_replay_events",
]
