"""Local-DB coordination primitives for conservative provider request pacing."""

from __future__ import annotations

from typing import Any


def provider_request_spacing_seconds(rate_limit_per_minute: int, min_interval_seconds: float = 0.0) -> float:
    """Return the conservative spacing implied by a provider's effective limit."""
    rate = max(1, int(rate_limit_per_minute))
    return max(60.0 / rate, max(0.0, float(min_interval_seconds)))


def reserve_provider_rate_limit_slot(connection: Any, provider_key: str, spacing_seconds: float,
                                     max_wait_seconds: float) -> float | None:
    """Atomically reserve one cross-process provider start slot.

    ``None`` means the existing shared queue already extends beyond the local
    caller's bounded wait budget, and *no* new slot was consumed.  A numeric
    result is the delay before this request may start.  This is intentionally
    stricter than a rolling burst allowance: providers are protected even when
    two service replicas start at the same time.
    """
    spacing = max(0.01, min(60.0, float(spacing_seconds)))
    max_wait = max(0.0, min(30.0, float(max_wait_seconds)))
    row = connection.execute(
        """INSERT INTO quant.provider_rate_limit_slots(provider_key,next_allowed_at,updated_at)
               VALUES(%s,clock_timestamp()+make_interval(secs => %s::double precision),clock_timestamp())
               ON CONFLICT(provider_key) DO UPDATE SET
                 next_allowed_at=GREATEST(quant.provider_rate_limit_slots.next_allowed_at,clock_timestamp())
                                 +make_interval(secs => %s::double precision),
                 updated_at=clock_timestamp()
               WHERE quant.provider_rate_limit_slots.next_allowed_at
                     <=clock_timestamp()+make_interval(secs => %s::double precision)
               RETURNING GREATEST(0::double precision,
                 EXTRACT(EPOCH FROM next_allowed_at-make_interval(secs => %s::double precision)-clock_timestamp())
               ) AS wait_seconds""",
        (str(provider_key), spacing, spacing, max_wait, spacing),
    ).fetchone()
    if row is None:
        return None
    return max(0.0, float(row["wait_seconds"] or 0.0))


__all__ = ["provider_request_spacing_seconds", "reserve_provider_rate_limit_slot"]
