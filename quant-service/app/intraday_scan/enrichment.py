"""Bounded evidence queues that reserve capacity for this run's lane leaders."""

from __future__ import annotations


FRONT_PER_LANE = 8
EVIDENCE_BUDGET = 128


def _round_robin(pools):
    ordered = []
    seen = set()
    for rank in range(max((len(pool) for pool in pools), default=0)):
        for pool in pools:
            if rank < len(pool) and pool[rank] not in seen:
                seen.add(pool[rank])
                ordered.append(pool[rank])
    return ordered


def _allocate(priority, old, fallback, limit):
    selected = list(dict.fromkeys([*priority, *old, *fallback]))[:limit]
    included = set(selected)
    return selected, {
        "budget": limit,
        "priority_total": len(set(priority)),
        "priority_missing": [symbol for symbol in dict.fromkeys(priority) if symbol not in included],
        "old_display_deferred": len(set(old) - included),
        "fallback_deferred": len(set(fallback) - included),
    }


def history_queue(scan, seeds, prefilter, *, limit=EVIDENCE_BUDGET):
    """Protect current nine-lane leaders and formal recommendations first."""
    live_pools = [[row["symbol"] for row in lane.get("tracking_candidates", [])[:FRONT_PER_LANE]]
                  for lane in scan.get("lanes", [])]
    live = _round_robin(live_pools)
    formal = [row["symbol"] for row in seeds if row.get("formal_recommendation")]
    old = [row["symbol"] for row in seeds if row.get("display_rank") is not None
           and not row.get("formal_recommendation")]
    # The advanced strategies need the OHLC prefilter before historical display
    # rows.  Otherwise a large carried watchlist can suppress a new setup.
    selected, coverage = _allocate([*live, *formal], prefilter, old, limit)
    coverage['old_display_deferred'] = len(set(old) - set(selected))
    coverage['prefilter_deferred'] = len(set(prefilter) - set(selected))
    return selected, coverage


def minute_queue(lanes, seeds, *, limit=EVIDENCE_BUDGET):
    """Use enriched same-run ranking, then follow historical display names."""
    pools = []
    for lane in lanes:
        current = [row for row in lane.get("items", []) if row.get("formal_rank") is not None]
        current.sort(key=lambda row: (-(row.get("formal_rank") or 0), row["symbol"]))
        pools.append([row["symbol"] for row in current[:FRONT_PER_LANE]])
    live = _round_robin(pools)
    formal = [row["symbol"] for row in seeds if row.get("formal_recommendation")]
    old = [row["symbol"] for row in seeds if row.get("display_rank") is not None
           and not row.get("formal_recommendation")]
    fallback = _round_robin([[row["symbol"] for row in lane.get("items", [])]
                             for lane in lanes])
    return _allocate([*live, *formal], old, fallback, limit)
