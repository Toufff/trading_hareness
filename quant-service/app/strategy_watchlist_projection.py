"""Compact, holdings-independent projection of the latest post-close scan.

The persisted post-close envelope contains replay evidence for every strategy
lane and can be several megabytes.  Personal decision surfaces only need the
small, deduplicated observation list.  Keeping this projection pure makes the
sync and async read paths behave identically and prevents broker state from
affecting market-wide discovery.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_REVIEW_PRIORITY = {
    "retain_watch": 0,
    "downgrade_watch": 2,
    "exclude": 3,
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _number(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _tracking_tags(metadata: dict[str, Any]) -> list[dict[str, str]]:
    tags: list[dict[str, str]] = []
    for value in _items(metadata.get("tracking_tags")):
        key = str(value.get("key") or "")
        if not key or value.get("active") is False:
            continue
        source = str(value.get("source") or "user")
        if source != "user":
            continue
        tags.append({
            "key": key,
            "label": str(value.get("label") or "用户主动跟踪"),
            "source": "user",
        })
    return tags


def compact_post_close_watchlist(
    payload: dict[str, Any],
    limit: int = 16,
    *,
    user_tracking: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a bounded observation list without implying trade authorization.

    Explicit user tracking is a durable source dimension, not a strategy lane.
    A later strategy hit adds a tag to the same symbol and can never erase the
    user's tracking tag.
    """

    latest_completed = _mapping(payload.get("latest_completed"))
    lane_summary = _mapping(_mapping(latest_completed.get("summary")).get("strategy_lanes"))
    reviews = {
        str(item.get("symbol")): item
        for item in _items(lane_summary.get("company_reviews"))
        if item.get("symbol")
    }
    merged: dict[str, dict[str, Any]] = {}
    for lane in _items(lane_summary.get("lanes")):
        lane_key = str(lane.get("key") or "")
        lane_label = str(lane.get("label") or lane_key or "未命名策略")
        # New independent discovery includes restricted/near-limit leaders.
        # Old stored runs retain their original selected-list interpretation.
        observations = lane.get('observation_list') if 'observation_list' in lane else lane.get('selected')
        for rank, candidate in enumerate(_items(observations), start=1):
            symbol = str(candidate.get("symbol") or "")
            if not symbol:
                continue
            item = merged.setdefault(symbol, {
                "symbol": symbol,
                "name": str(candidate.get("name") or symbol),
                "lane_keys": [],
                "lane_labels": [],
                "lane_ranks": [],
                "tags": [],
                "user_requested_tracking": False,
                "reason": candidate.get("reason"),
                "confirmation": candidate.get("confirmation"),
                "invalidation": candidate.get("invalidation"),
                "caution": candidate.get("caution") or candidate.get("risk"),
                "sector_label": candidate.get("sector_label"),
                "rank_score": _number(candidate.get("rank_score"), _number(candidate.get("raw_score"))),
                "buy_authorized": False,
                "depends_on_holdings": False,
                "execution": candidate.get('execution'),
                "observation_state": candidate.get('state'),
            })
            if lane_key and lane_key not in item["lane_keys"]:
                item["lane_keys"].append(lane_key)
                item["lane_labels"].append(lane_label)
                item["lane_ranks"].append(rank)
                item["tags"].append({
                    "key": f"strategy:{lane_key}",
                    "label": lane_label,
                    "source": "strategy",
                })
            item["rank_score"] = max(item["rank_score"], _number(candidate.get("rank_score"), _number(candidate.get("raw_score"))))

    strategy_total_unique = len(merged)
    tracked_symbols: set[str] = set()
    for tracked in user_tracking or []:
        row = _mapping(tracked)
        symbol = str(row.get("symbol") or "")
        metadata = _mapping(row.get("metadata"))
        tags = _tracking_tags(metadata)
        if not symbol or not tags:
            continue
        tracked_symbols.add(symbol)
        analysis = _mapping(metadata.get("tracking_analysis"))
        tracking_research = _mapping(metadata.get("user_tracking_research"))
        item = merged.setdefault(symbol, {
            "symbol": symbol,
            "name": str(row.get("label") or symbol),
            "lane_keys": [],
            "lane_labels": [],
            "lane_ranks": [],
            "tags": [],
            "user_requested_tracking": True,
            "reason": analysis.get("reason"),
            "confirmation": analysis.get("confirmation"),
            "invalidation": analysis.get("invalidation"),
            "caution": analysis.get("caution"),
            "sector_label": metadata.get("sector_label"),
            "rank_score": 0.0,
            "buy_authorized": False,
            "depends_on_holdings": False,
            "tracking_research": tracking_research or None,
        })
        item["user_requested_tracking"] = True
        if tracking_research:
            item["tracking_research"] = tracking_research
        if row.get("label"):
            item["name"] = str(row["label"])
        for field in ("reason", "confirmation", "invalidation", "caution"):
            if not item.get(field) and analysis.get(field):
                item[field] = analysis[field]
        manual_keys = {manual["key"] for manual in tags}
        item["tags"] = tags + [
            tag for tag in _items(item.get("tags"))
            if tag.get("key") not in manual_keys
        ]

    for symbol, item in merged.items():
        review = _mapping(reviews.get(symbol))
        selection = _mapping(review.get("selection"))
        disposition = str(selection.get("disposition") or "technical_observation")
        if not item["lane_keys"] and item["user_requested_tracking"] and not review:
            disposition = "user_tracking"
        item["review_status"] = disposition
        item["review_label"] = {
            "retain_watch": "公司复核后保留观察",
            "downgrade_watch": "公司复核后降级观察",
            "exclude": "公司复核未通过，仍按用户要求跟踪" if item["user_requested_tracking"] else "公司复核后排除",
            "technical_observation": "量价观察，尚未升级",
            "user_tracking": (
                "人工跟踪研究已更新" if _mapping(item.get("tracking_research")).get("status") in {"complete", "degraded"}
                else "人工跟踪研究数据不可用"
            ),
        }.get(disposition, "量价观察，尚未升级")
        item["company_conclusion"] = review.get("conclusion")
        item["company_risk"] = review.get("risk")
        item["business"] = review.get("business")

    candidates = [
        item for item in merged.values()
        if item["review_status"] != "exclude" or item["user_requested_tracking"]
    ]
    candidates.sort(key=lambda item: (
        0 if item["user_requested_tracking"] else 1,
        _REVIEW_PRIORITY.get(str(item["review_status"]), 1),
        -len(item["lane_keys"]),
        min(item["lane_ranks"] or [999]),
        -_number(item["rank_score"]),
        item["symbol"],
    ))
    bounded_limit = max(1, min(int(limit or 16), 50))
    review_coverage = _mapping(lane_summary.get("review_coverage"))
    return {
        "recommendation_pool": _mapping(latest_completed.get('summary')).get('recommendation_pool'),
        "as_of_date": lane_summary.get("as_of_date") or latest_completed.get("as_of_date"),
        "status": lane_summary.get("status") or ("completed" if latest_completed else "unavailable"),
        "research_only": True,
        "depends_on_holdings": False,
        "total_unique": len(candidates),
        "strategy_total_unique": strategy_total_unique,
        "user_tracking_total": len(tracked_symbols),
        "items": candidates[:bounded_limit],
        "review_coverage": {
            "planned": review_coverage.get("planned"),
            "completed": review_coverage.get("completed"),
            "missing_symbols": review_coverage.get("missing_symbols") or [],
        },
        "notice": "用户主动跟踪与策略命中是独立标签；全市场观察与账户持仓完全独立，不构成买入授权。",
    }


__all__ = ["compact_post_close_watchlist"]
