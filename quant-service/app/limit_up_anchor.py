"""Live limit-up anchors computed locally from the licensed all-A snapshot.

The intraday linkage miner needs one factual input: which names are sealed at
their limit right now.  This used to be fetched from an Eastmoney HTML pool
via AKShare, whose lxml parser segfaulted the edge collector at session
boundaries five times in one week (an abandoned worker thread kept parsing
after its 20s timeout, then took the GIL on a dead thread state:
``htmlParseChunk -> PyGILState_Ensure -> pthread_mutex_lock(0x50)``).

The same fact is already in-process: the fuyao all-A snapshot and the
session's ``stk_limit`` prices decide sealed-ness for the leader pool every
scan, and that computation covered 100% of the day's true sealed boards when
measured against the settled close on 2026-08-27.  Deriving the anchor rows
locally removes the HTML fetch, the lxml dependency and the crash class in
one move, and the anchor exists exactly when the linkage miner runs instead
of when a vendor page happens to answer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .xiaojie_indicators import LIMIT_TOLERANCE, snapshot_fields

#: Same bound the Eastmoney pool path applied before persisting.
MAX_ANCHOR_ROWS = 300


def live_limit_up_pool_rows(
    snapshot_rows: list[dict[str, Any]],
    limits: Mapping[str, float],
    names: Mapping[str, str] | None,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    """Anchor rows for every name currently sealed at its limit price.

    Sealed means the last price is at the limit, the same criterion the
    leader pool applies - a name that merely touched the limit intraday is a
    broken board, not an anchor.  Rows carry the ``market_events`` shape the
    linkage relation query reads (``event_type='limit_up_pool'`` on the
    session date), so downstream consumers see no change of contract.
    """
    events: list[dict[str, Any]] = []
    published_at = observed_at.isoformat()
    for row in snapshot_rows:
        symbol = str(row.get("symbol") or "")
        limit_up = limits.get(symbol)
        if not symbol or limit_up is None or limit_up <= 0:
            continue
        fields = snapshot_fields(row)
        price = fields.get("price")
        if price is None or price < limit_up - LIMIT_TOLERANCE:
            continue
        name = (names or {}).get(symbol) or symbol
        events.append({
            "ts_code": symbol,
            "event_type": "limit_up_pool",
            "published_at": published_at,
            "title": f"limit_up_pool：{name}",
            "url": None,
            "upstream_site": "fuyao_derived",
            "raw": {"price": price, "limit_up": limit_up,
                    "pct_change": row.get("pct_change"), "high": fields.get("high"),
                    "sealed": True, "source": "fuyao_all_a_plus_stk_limit"},
        })
        if len(events) >= MAX_ANCHOR_ROWS:
            break
    return events


__all__ = ["MAX_ANCHOR_ROWS", "live_limit_up_pool_rows"]
