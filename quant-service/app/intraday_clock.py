"""Deterministic China-session clock helpers for live and replay features."""

from __future__ import annotations

import re
from datetime import time
from typing import Any


def feature_clock(value: Any) -> time | None:
    """Return a China-session clock from ``HH:MM`` or provider timestamps."""
    text = str(value or "").strip()
    matched = re.findall(r"(?:T|\s)(\d{2}):?(\d{2})(?::\d{2})?(?:\D|$)", text)
    if matched:
        hour, minute = matched[-1]
    else:
        compact = re.fullmatch(r"(\d{2}):?(\d{2})(?::\d{2})?", text)
        if not compact:
            return None
        hour, minute = compact.groups()[:2]
    if not matched:
        matched = [(hour, minute)]
    try:
        return time(int(hour), int(minute))
    except ValueError:
        return None


def eac_window(value: Any) -> str:
    """Classify a minute without treating a late-session spike as fresh momentum."""
    clock = feature_clock(value)
    if clock is None:
        return "unknown"
    if time(9, 40) <= clock <= time(10, 45):
        return "morning"
    if time(13, 0) <= clock <= time(14, 20):
        return "afternoon"
    return "late_or_opening"


def minute_bucket(value: Any) -> str | None:
    clock = feature_clock(value)
    return clock.strftime("%H:%M") if clock is not None else None


__all__ = ["eac_window", "feature_clock", "minute_bucket"]
