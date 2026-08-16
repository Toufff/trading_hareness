"""Bounded retention for redundant intraday signal observations.

This module deliberately preserves every event that can be a user-facing or
research outcome record. Only repeated non-confirmed observations are
ephemeral: their minimal causal rule inputs have already been frozen in
``intraday_rule_input_snapshots`` for the same forward evidence window.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any


DEFAULT_EPHEMERAL_SIGNAL_RETENTION_DAYS = 90
MIN_EPHEMERAL_SIGNAL_RETENTION_DAYS = 60
MAX_EPHEMERAL_SIGNAL_RETENTION_DAYS = 180


def ephemeral_signal_retention_days(environ: Mapping[str, str] | None = None) -> int:
    """Return a conservative, bounded retention period for noisy event rows.

    The minimum matches the independent-day validation gate. Operators cannot
    configure a small value that silently removes a full forward evidence
    window before it can be inspected.
    """
    source = os.environ if environ is None else environ
    try:
        value = int(str(source.get("INTRADAY_EPHEMERAL_SIGNAL_RETENTION_DAYS", DEFAULT_EPHEMERAL_SIGNAL_RETENTION_DAYS)))
    except (TypeError, ValueError):
        value = DEFAULT_EPHEMERAL_SIGNAL_RETENTION_DAYS
    return max(MIN_EPHEMERAL_SIGNAL_RETENTION_DAYS, min(MAX_EPHEMERAL_SIGNAL_RETENTION_DAYS, value))


def prune_ephemeral_signal_events(connection: Any, *, cutoff: datetime) -> dict[str, int]:
    """Delete only redundant, un-delivered non-confirmed observations.

    ``confirmed``/``alerted`` events, any delivery record, and any outcome
    remain even if a later workflow changes event semantics. This protects the
    audit, paper-decision and settlement paths while preventing a 30-second
    persistent setup from growing forever.
    """
    rows = connection.execute(
        """DELETE FROM quant.intraday_signal_events event
              WHERE event.observed_at<%s
                AND event.state IN ('suppressed','confirming','detected','invalidated')
                AND NOT EXISTS (
                    SELECT 1 FROM quant.intraday_signal_outcomes outcome
                     WHERE outcome.signal_event_id=event.signal_event_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM quant.intraday_alert_deliveries delivery
                     WHERE delivery.signal_event_id=event.signal_event_id
                )
            RETURNING event.state""",
        (cutoff,),
    ).fetchall()
    result = {"suppressed": 0, "confirming": 0, "detected": 0, "invalidated": 0}
    for row in rows:
        state = str(row["state"])
        if state in result:
            result[state] += 1
    result["total"] = sum(result.values())
    return result


__all__ = [
    "DEFAULT_EPHEMERAL_SIGNAL_RETENTION_DAYS",
    "MAX_EPHEMERAL_SIGNAL_RETENTION_DAYS",
    "MIN_EPHEMERAL_SIGNAL_RETENTION_DAYS",
    "ephemeral_signal_retention_days",
    "prune_ephemeral_signal_events",
]
