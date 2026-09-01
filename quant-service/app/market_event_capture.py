"""Capture Fuyao all-A auction and limit-pool evidence.

This module contains no strategy decisions.  It converts the documented
Fuyao payloads into the existing ``market_events`` evidence shape and leaves
the provider call, cadence and database executor to the application.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


CAPTURE_CAPABILITIES: tuple[str, ...] = (
    "a_share_limit_up_pool", "a_share_limit_break_pool", "a_share_limit_up_ladder",
)


def _items(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    value = (data or {}).get("item")
    return [dict(row) for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def normalize_fuyao_events(capability: str, data: Mapping[str, Any] | None,
                           observed_at: datetime) -> list[dict[str, Any]]:
    """Normalize one Fuyao capability while preserving its raw item."""
    events: list[dict[str, Any]] = []
    if capability == "a_share_limit_up_ladder":
        boards = (data or {}).get("boards")
        # Some responses put boards under each dated item; accept both shapes.
        dated = _items(data)
        board_sets = [boards] if isinstance(boards, dict) else [item.get("boards") for item in dated]
        for board_set in board_sets:
            if not isinstance(board_set, dict):
                continue
            for bucket, values in board_set.items():
                for item in values if isinstance(values, list) else []:
                    if not isinstance(item, dict) or not item.get("thscode"):
                        continue
                    symbol = str(item["thscode"]).upper()
                    board_num = item.get("board_num")
                    events.append({
                        "ts_code": symbol, "event_type": "limit_chain",
                        "published_at": observed_at.isoformat(),
                        "title": f"连板链：{item.get('name') or symbol} {board_num or bucket}",
                        "url": None, "event_identity_key": f"fuyao_ths:limit_chain:{symbol}:{observed_at.astimezone(CN_TZ).date().isoformat()}:{board_num or bucket}",
                        "raw": {"capability": capability, "bucket": bucket, **item},
                    })
        return events

    event_type = {
        "a_share_limit_up_pool": "limit_up_pool",
        "a_share_limit_break_pool": "limit_open_pool",
    }.get(capability)
    if event_type is None:
        return []
    for item in _items(data):
        symbol = str(item.get("thscode") or "").upper()
        if not symbol:
            continue
        label = "涨停池" if event_type == "limit_up_pool" else "炸板池"
        events.append({
            "ts_code": symbol, "event_type": event_type,
            "published_at": observed_at.isoformat(),
            "title": f"{label}：{item.get('name') or symbol}", "url": None,
            # Include the observed minute: pool membership changes during the
            # session must remain separate evidence, not an upserted snapshot.
            "event_identity_key": f"fuyao_ths:{event_type}:{symbol}:{observed_at.strftime('%Y%m%d%H%M')}",
            "raw": {"capability": capability, **item},
        })
    return events


def normalize_fuyao_auction(data: Mapping[str, Any] | None, observed_at: datetime) -> list[dict[str, Any]]:
    """Normalize the final all-A auction snapshot (one daily identity/name)."""
    events: list[dict[str, Any]] = []
    for item in _items(data):
        symbol = str(item.get("thscode") or "").upper()
        if not symbol:
            continue
        events.append({
            "ts_code": symbol, "event_type": "auction_final",
            "published_at": observed_at.isoformat(),
            "title": f"收盘集合竞价：{item.get('name') or symbol}", "url": None,
            "event_identity_key": f"fuyao_ths:auction_final:{symbol}:{observed_at.strftime('%Y%m%d')}",
            "raw": {"capability": "a_share_auction_snapshot", **item,
                    "auction_phase": (data or {}).get("auction_phase"),
                    "data_status": (data or {}).get("data_status")},
        })
    return events


async def capture(
    observed_at: datetime,
    *,
    fetch: Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]],
    persist: Callable[[str, list[dict[str, Any]]], Awaitable[int]],
    include_auction: bool = False,
    auction_symbols: Sequence[str] = (),
) -> dict[str, Any]:
    """Fetch and persist the bounded event set, continuing after one failure."""
    results: dict[str, Any] = {"status": "completed", "capabilities": {}, "stored": 0}
    for capability in CAPTURE_CAPABILITIES:
        try:
            data = await fetch(capability, {})
            rows = normalize_fuyao_events(capability, data, observed_at)
            stored = await persist("fuyao_ths", rows) if rows else 0
            results["capabilities"][capability] = {"status": "completed", "received": len(rows), "stored": stored}
            results["stored"] += stored
        except Exception as error:  # noqa: BLE001 - one pool outage must not stop the others
            results["status"] = "partial"
            results["capabilities"][capability] = {"status": "failed", "error": str(error)[:240]}
    if include_auction and auction_symbols:
        received = stored = 0
        failures: list[str] = []
        # Provider query strings remain bounded; a large all-A snapshot is
        # split into deterministic chunks and merged only as evidence rows.
        for offset in range(0, len(auction_symbols), 500):
            chunk = [str(item).upper() for item in auction_symbols[offset:offset + 500]]
            try:
                data = await fetch("a_share_auction_snapshot", {"thscodes": ",".join(chunk)})
                rows = normalize_fuyao_auction(data, observed_at)
                received += len(rows)
                stored += await persist("fuyao_ths", rows) if rows else 0
            except Exception as error:  # noqa: BLE001
                failures.append(str(error)[:180])
        results["auction"] = {"status": "completed" if not failures else "partial", "requested": len(auction_symbols), "received": received, "stored": stored, "failures": failures}
        results["stored"] += stored
        if failures:
            results["status"] = "partial"
    return results


__all__ = ["CAPTURE_CAPABILITIES", "capture", "normalize_fuyao_auction", "normalize_fuyao_events"]
