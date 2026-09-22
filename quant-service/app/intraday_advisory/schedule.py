"""Deterministic advisory cadence; no model is used to decide when to run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class ScheduleDecision:
    fetch_quotes: bool = False
    run_deepseek: bool = False
    run_codex: bool = False
    report_kind: str | None = None


def _continuous(now: datetime) -> bool:
    value = now.time().replace(tzinfo=None)
    return time(9, 30) <= value <= time(11, 30) or time(13, 0) <= value <= time(15, 0)


def decide(now: datetime, *, last_fetch: datetime | None, last_deepseek: datetime | None,
           last_codex: datetime | None) -> ScheduleDecision:
    """Three briefings; other ten-minute slots check changed conditions silently.

    The two-minute windows tolerate a busy worker. Durable delivery keys in the
    runtime prevent duplicates across restarts; an attempt can retry after 60s.
    """
    if now.weekday() >= 5:
        return ScheduleDecision()
    fetch = _continuous(now) and (last_fetch is None or (now - last_fetch).total_seconds() >= 5)
    minute = now.hour * 60 + now.minute
    special = next((kind for start, kind in ((600,'fixed'),(695,'midday'),(885,'tail'))
                    if start <= minute < start+2), None)
    codex_due = special is not None
    if last_codex is not None and (now - last_codex).total_seconds() < 60:
        codex_due = False
    ten_minute_slot = _continuous(now) and now.minute % 10 == 0 and minute not in {570,690,900}
    deepseek_due = ten_minute_slot and special is None and (
        last_deepseek is None or (now - last_deepseek).total_seconds() >= 9 * 60
    )
    return ScheduleDecision(fetch, deepseek_due, codex_due,
                            special)


__all__ = ["ScheduleDecision", "decide"]
