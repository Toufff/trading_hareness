"""Provider-free deterministic replay primitives for recorded intraday events.

This is the P2 event clock, not a historical downloader.  Callers supply only
events already retained locally; ordering is by *availability* rather than
exchange time so the replay cannot consume a later-arriving provider result
before the original live scan could have seen it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .strategy_contracts import MarketEvent


REPLAY_VERSION = "intraday-event-replay-v1"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def replay_order_key(event: MarketEvent, *, source_sequence: int = 0) -> tuple[datetime, int, str]:
    """Stable replay key: availability clock, source order, then immutable id."""
    return _as_utc(event.available_at), max(0, int(source_sequence or 0)), str(event.event_id)


def market_event_from_row(row: dict[str, Any], *, source: str, schema_version: str,
                          payload: dict[str, Any] | None = None) -> MarketEvent:
    """Adapt a persisted row without inventing timestamps or provider calls."""
    available_at = row.get("available_at") or row.get("observed_at") or row.get("ingested_at")
    if not isinstance(available_at, datetime):
        raise ValueError("replay event requires persisted available_at or observed_at")
    ingested_at = row.get("ingested_at") or available_at
    if not isinstance(ingested_at, datetime):
        raise ValueError("replay event requires a datetime ingested_at")
    event_time = row.get("event_time") or row.get("bar_time") or row.get("observed_at")
    if event_time is not None and not isinstance(event_time, datetime):
        event_time = None
    raw_payload = payload if payload is not None else (row.get("raw") if isinstance(row.get("raw"), dict) else {})
    stable = json.dumps(raw_payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return MarketEvent(
        event_id=str(row.get("event_id") or row.get("observation_id") or row.get("id") or hashlib.sha256(
            f"{source}:{row.get('symbol')}:{available_at.isoformat()}:{stable}".encode()
        ).hexdigest()),
        schema_version=schema_version, source=source, symbol=(str(row["symbol"]) if row.get("symbol") else None),
        event_time=event_time, available_at=_as_utc(available_at), ingested_at=_as_utc(ingested_at),
        payload_hash=hashlib.sha256(stable.encode()).hexdigest(),
        quality_flags=tuple(str(item) for item in row.get("quality_flags", ()) if str(item)),
    )


def replay_events(
    events: Iterable[tuple[MarketEvent, int, dict[str, Any]]],
    transition: Callable[[dict[str, Any], MarketEvent, dict[str, Any]], tuple[dict[str, Any], Any]],
    *, initial_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a pure transition over locally recorded events deterministically.

    The digest is suitable for a golden-day regression: identical input events
    and strategy implementation must produce the same transition trace.
    """
    ordered = sorted(events, key=lambda item: replay_order_key(item[0], source_sequence=item[1]))
    state = dict(initial_state or {})
    trace: list[dict[str, Any]] = []
    for event, sequence, payload in ordered:
        if event.available_at > event.ingested_at:
            raise ValueError("event available_at cannot be after ingested_at")
        state, output = transition(dict(state), event, dict(payload))
        trace.append({
            "event_id": event.event_id, "source": event.source, "symbol": event.symbol,
            "available_at": event.available_at.isoformat(), "source_sequence": int(sequence),
            "payload_hash": event.payload_hash, "output": output,
        })
    canonical = json.dumps({"version": REPLAY_VERSION, "trace": trace, "state": state}, sort_keys=True,
                           ensure_ascii=False, default=str, separators=(",", ":"))
    return {
        "version": REPLAY_VERSION, "events": len(ordered), "state": state, "trace": trace,
        "digest": hashlib.sha256(canonical.encode()).hexdigest(),
        "policy": "recorded-events-only; no provider access, no threshold fitting, no order submission",
    }


__all__ = ["REPLAY_VERSION", "market_event_from_row", "replay_events", "replay_order_key"]
