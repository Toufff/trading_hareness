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
    """Five-second acquisition, aligned ten-minute checks and thirty-minute reports.

    Every third ten-minute slot belongs to Codex; DeepSeek runs only on the two
    intervening slots.  11:35 and 14:45 replace the nearest 11:30 and 14:30
    fixed reports, so the user does not receive duplicate summaries around the
    special checkpoints.
    """
    if now.weekday() >= 5:
        return ScheduleDecision()
    fetch = _continuous(now) and (last_fetch is None or (now - last_fetch).total_seconds() >= 5)
    minute = now.hour * 60 + now.minute
    special = "midday" if 11 * 60 + 35 <= minute < 11 * 60 + 37 else "tail" if 14 * 60 + 45 <= minute < 14 * 60 + 47 else None
    regular_slot = _continuous(now) and now.minute in {0, 30}
    suppressed = (now.hour == 11 and now.minute == 30) or (now.hour == 14 and now.minute == 30)
    codex_due = special is not None or (regular_slot and not suppressed)
    if last_codex is not None and (now - last_codex).total_seconds() < (120 if special else 1500):
        codex_due = False
    ten_minute_slot = _continuous(now) and now.minute % 10 == 0 and not suppressed
    deepseek_due = ten_minute_slot and not codex_due and (
        last_deepseek is None or (now - last_deepseek).total_seconds() >= 9 * 60
    )
    # A full Codex checkpoint replaces every third DeepSeek check.  This keeps
    # the model rhythm deterministic across restarts and prevents a restart at
    # an arbitrary second from creating an extra ten-minute message.
    return ScheduleDecision(fetch, deepseek_due, codex_due,
                            special or ("fixed" if codex_due else None))


__all__ = ["ScheduleDecision", "decide"]
