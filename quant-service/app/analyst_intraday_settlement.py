"""Shared China-session bounds for analyst intraday research ledgers.

The two analyst clocks deliberately remain separate: an observation settles
from immutable local availability, while an author-stated action is a
retrospective replay artifact.  Both use the same bounded local quote window
so neither can borrow lunch or overnight prices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .intraday_clock import intraday_outcome_window


ENTRY_QUOTE_TOLERANCE_SECONDS = 90
EXIT_QUOTE_TOLERANCE_SECONDS = 90


def json_safe_window(window: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in window.items()
    }


def entry_quote_window(clock_at: datetime, *, cutoff_at: datetime) -> dict[str, Any]:
    """Return the bounded first-local-quote window for either analyst clock."""
    window = intraday_outcome_window(
        clock_at,
        horizon_minutes=0,
        cutoff=cutoff_at,
        tolerance_seconds=ENTRY_QUOTE_TOLERANCE_SECONDS,
    )
    reason = str(window.get("reason") or "")
    if reason == "exit_quote_missing_within_tolerance":
        window["reason"] = "entry_quote_missing_within_tolerance"
    elif reason == "awaiting_exit_quote_within_tolerance":
        window["reason"] = "awaiting_entry_quote_within_tolerance"
    return window


def has_bounded_query_window(window: dict[str, Any]) -> bool:
    """True if it is safe to inspect the persisted interval after the fact."""
    return bool(
        window.get("query_start") is not None and window.get("query_end") is not None
        and window["query_end"] >= window["query_start"]
    )


__all__ = [
    "ENTRY_QUOTE_TOLERANCE_SECONDS", "EXIT_QUOTE_TOLERANCE_SECONDS", "entry_quote_window",
    "has_bounded_query_window", "json_safe_window",
]
