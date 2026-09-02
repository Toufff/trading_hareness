"""Backfill deterministic intraday signal attribution after a classifier fix.

Signal evidence is immutable, but attribution is a derived research label.
Rebuilding it in the same transaction as outcome settlement prevents old
labels from contaminating subsequent offline policy reviews.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: Bound the per-call backfill window (audit E/J: unbounded read + N row
#: UPDATEs). A restart or the next scheduled settlement picks up whatever a
#: single call did not reach; this is a backfill of a derived label, not the
#: immutable evidence itself, so partial progress across calls is safe.
INTRADAY_SIGNAL_ATTRIBUTION_BACKFILL_LIMIT = 20_000


@dataclass(frozen=True)
class IntradaySignalAttributionRefreshDependencies:
    attribution_for: Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any]]
    json_safe: Callable[[Any], Any]
    backfill_limit: int = INTRADAY_SIGNAL_ATTRIBUTION_BACKFILL_LIMIT


def refresh_intraday_signal_attributions(
    connection: Any, *, cutoff: datetime, dependencies: IntradaySignalAttributionRefreshDependencies,
) -> int:
    """Backfill deterministic attribution after a classifier correction.

    Changed rows are written with one batched ``UPDATE ... FROM unnest(...)``
    instead of one ``UPDATE`` per row.
    """
    rows = connection.execute(
        """SELECT signal_event_id,signal_key,signal_type,conditions,evidence
             FROM quant.intraday_signal_events
            WHERE state IN ('confirmed','alerted')
              AND signal_type IN ('entry','watch','reduce','exit')
              AND observed_at<=%s
            ORDER BY observed_at
            LIMIT %s""",
        (cutoff, dependencies.backfill_limit),
    ).fetchall()
    changed_ids: list[Any] = []
    changed_evidence: list[str] = []
    for row in rows:
        evidence = dict(row["evidence"] or {})
        attribution = dependencies.attribution_for(
            str(row["signal_key"]), str(row["signal_type"]),
            dict(row["conditions"] or {}), evidence,
        )
        if evidence.get("attribution") == attribution:
            continue
        evidence["attribution"] = attribution
        changed_ids.append(row["signal_event_id"])
        changed_evidence.append(json.dumps(dependencies.json_safe(evidence)))
    if not changed_ids:
        return 0
    connection.execute(
        """UPDATE quant.intraday_signal_events AS t
              SET evidence=v.evidence::jsonb
             FROM unnest(%s::uuid[],%s::text[]) AS v(signal_event_id,evidence)
            WHERE t.signal_event_id=v.signal_event_id""",
        (changed_ids, changed_evidence),
    )
    return len(changed_ids)


__all__ = [
    "INTRADAY_SIGNAL_ATTRIBUTION_BACKFILL_LIMIT",
    "IntradaySignalAttributionRefreshDependencies",
    "refresh_intraday_signal_attributions",
]
