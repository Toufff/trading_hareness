"""Pure ten-session, board-local ranking for shadow research.

The module knows nothing about databases, schedulers, HTTP, analyst opinions or
orders.  It accepts an already point-in-time-bounded daily-bar projection and
returns a deterministic candidate pool.  Coverage and adjusted-history gates
fail closed before any rank is emitted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
from typing import Any

from .market_rules import is_at_limit


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def price_band_board(symbol: str) -> str | None:
    """Map an A-share symbol to the workbook's three price-band boards."""
    value = str(symbol or "").upper()
    code = value.split(".", 1)[0]
    if value.endswith(".BJ") or code.startswith(("4", "8")):
        return "bj"
    if value.endswith(".SZ") and code.startswith(("300", "301")):
        return "growth"
    if value.endswith(".SH") and code.startswith(("688", "689")):
        return "growth"
    if value.endswith((".SH", ".SZ")):
        return "main"
    return None


def rank_ten_day_candidates(
    daily_rows: list[dict[str, Any]],
    as_of_date: date,
    *,
    daily_symbols: int,
    minimum_full_market_symbols: int,
    per_board_limit: int = 30,
) -> dict[str, Any]:
    """Rank complete adjusted ten-session returns inside each price-band board."""
    bounded_limit = max(1, min(int(per_board_limit), 30))
    source_status: dict[str, Any] = {
        "daily_symbols": int(daily_symbols),
        "minimum_full_market_symbols": int(minimum_full_market_symbols),
        "required_sessions": 11,
        "return_horizon_sessions": 10,
    }
    if int(daily_symbols) < int(minimum_full_market_symbols):
        return {
            "status": "blocked", "reason": "insufficient_same_date_daily_coverage",
            "source_status": {**source_status, "eligible_symbols": 0}, "candidates": [],
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in daily_rows:
        symbol = str(raw.get("symbol") or "").upper()
        if price_band_board(symbol) is not None:
            grouped[symbol].append(dict(raw))

    eligible: list[dict[str, Any]] = []
    incomplete = 0
    for symbol, values in grouped.items():
        ordered = sorted(values, key=lambda row: row.get("trading_date") or date.min)
        if len(ordered) < 11 or ordered[-1].get("trading_date") != as_of_date:
            incomplete += 1
            continue
        window = ordered[-11:]
        adjusted = []
        valid = True
        for row in window:
            close = _number(row.get("close"))
            factor = _number(row.get("adj_factor"))
            if close is None or close <= 0 or factor is None or factor <= 0:
                valid = False
                break
            adjusted.append(close * factor)
        if not valid or not adjusted or adjusted[0] <= 0:
            incomplete += 1
            continue
        latest = window[-1]
        close = _number(latest.get("close"))
        pre_close = _number(latest.get("pre_close"))
        current_return = (close / pre_close - 1) * 100 if close is not None and pre_close not in (None, 0) else None
        if current_return is None:
            incomplete += 1
            continue
        limit_up = _number(latest.get("limit_up"))
        is_limit_up = is_at_limit(close, limit_up)
        ohlc = [_number(latest.get(key)) for key in ("open", "high", "low", "close")]
        is_one_word = bool(is_limit_up and None not in ohlc and max(ohlc) - min(ohlc) <= max(0.001, close * 0.0001))
        eligible.append({
            "symbol": symbol,
            "name": latest.get("name"),
            "board": price_band_board(symbol),
            "ten_day_return_pct": round((adjusted[-1] / adjusted[0] - 1) * 100, 6),
            "current_return_pct": round(current_return, 6),
            "is_limit_up": is_limit_up,
            "is_one_word_board": is_one_word,
            "recently_suspended": any(bool(row.get("is_suspended")) for row in window),
            "source_available_at": max(
                (row.get("available_at") for row in window if row.get("available_at") is not None),
                default=None,
            ),
        })

    source_status.update({"eligible_symbols": len(eligible), "incomplete_history_symbols": incomplete})
    if len(eligible) < int(minimum_full_market_symbols):
        return {
            "status": "blocked", "reason": "insufficient_complete_adjusted_ten_session_histories",
            "source_status": source_status, "candidates": [],
        }

    ranked: list[dict[str, Any]] = []
    board_counts: dict[str, int] = {}
    for board in ("main", "growth", "bj"):
        board_rows = sorted(
            (item for item in eligible if item["board"] == board),
            key=lambda item: (-float(item["ten_day_return_pct"]), str(item["symbol"])),
        )
        board_counts[board] = len(board_rows)
        ranked.extend({**item, "ten_day_rank": rank} for rank, item in enumerate(board_rows[:bounded_limit], start=1))
    source_status["eligible_by_board"] = board_counts
    return {"status": "completed", "reason": None, "source_status": source_status, "candidates": ranked}


__all__ = ["price_band_board", "rank_ten_day_candidates"]
