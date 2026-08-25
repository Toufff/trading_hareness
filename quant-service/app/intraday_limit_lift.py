"""Causal minute-path labels for post-close limit-up research."""

from __future__ import annotations

from typing import Any, Callable

from .strategy_thresholds import MAX_ENTRY_INTRADAY_GAIN_PCT


def intraday_limit_lift_pattern(
    rows: list[dict[str, Any]], daily: dict[str, Any], *,
    number: Callable[[Any], float | None],
    limit_ratio: Callable[[str, bool], float],
    minute_features: Callable[..., dict[str, Any] | None],
    session_rows: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    """Find causal opening-drive, ignition and deep-reversal checkpoints."""
    session = session_rows(rows, number=number)
    previous_close = number(daily.get("pre_close"))
    limit_pct = float(daily.get("limit_pct") or limit_ratio(str(daily.get("symbol") or ""), bool(daily.get("is_st"))) * 100)
    limit_price = number(daily.get("limit_up"))
    if limit_price is None and float(daily.get("close_pct") or 0) >= limit_pct * 0.95:
        limit_price = number(daily.get("close"))
    deep_discount_pct = -limit_pct * 0.85
    limit_reached_pct = limit_pct * 0.95
    if len(session) < 6 or previous_close is None or previous_close <= 0:
        return {"status": "insufficient_minute_data", "minute_rows": len(session), "curve": []}

    curve: list[dict[str, Any]] = []
    standard_ignition: dict[str, Any] | None = None
    reversal_impulse: dict[str, Any] | None = None
    session_low_pct, session_low_time, session_low_index = float("inf"), None, 0
    session_high_pct, session_high_time = float("-inf"), None
    total_volume = sum(float(number(row.get("volume_lot", row.get("vol"))) or 0) for row in session)

    for index, row in enumerate(session):
        price = float(row["close"])
        pct_change = (price / previous_close - 1) * 100
        if pct_change < session_low_pct:
            session_low_pct, session_low_time, session_low_index = pct_change, str(row["time"]), index
        if pct_change > session_high_pct:
            session_high_pct, session_high_time = pct_change, str(row["time"])
        feature = minute_features(session[:index + 1], source="tencent_free_pattern") if index >= 5 else None
        point = {"time": row["time"], "price": round(price, 4), "pct_vs_preclose": round(pct_change, 4),
                 "volume_lot": number(row.get("volume_lot", row.get("vol"))),
                 "return_3m_pct": (feature or {}).get("return_3m_pct"),
                 "minute_volume_multiple": (feature or {}).get("minute_volume_multiple"),
                 "above_vwap_pct": (feature or {}).get("above_vwap_pct")}
        curve.append(point)
        if not feature:
            continue
        return_3m, volume_multiple = number(feature.get("return_3m_pct")), number(feature.get("minute_volume_multiple"))
        above_vwap, breakout = number(feature.get("above_vwap_pct")), number(feature.get("breakout_above_prior_high_pct"))
        recovery = number(feature.get("recovery_from_session_low_pct"))
        evidence = {**point, "index": index, "recovery_from_session_low_pct": recovery,
                    "breakout_above_prior_high_pct": breakout}
        if (standard_ignition is None and 0.5 <= pct_change <= MAX_ENTRY_INTRADAY_GAIN_PCT
                and return_3m is not None and 1.2 <= return_3m <= 4.5
                and volume_multiple is not None and volume_multiple >= 2.5 and above_vwap is not None and above_vwap >= 0
                and breakout is not None and breakout >= 0):
            standard_ignition = evidence
        if (reversal_impulse is None and session_low_pct <= deep_discount_pct and pct_change <= 0.5
                and recovery is not None and recovery >= 3.0 and return_3m is not None and return_3m >= 1.2
                and volume_multiple is not None and volume_multiple >= 2.5 and above_vwap is not None and above_vwap >= 0):
            reversal_impulse = evidence

    def first_reclaim(level_pct: float, start_index: int = 0) -> dict[str, Any] | None:
        for index, point in enumerate(curve[start_index:], start=start_index):
            if float(point["pct_vs_preclose"]) >= level_pct:
                return {**point, "index": index}
        return None

    def first_price_reclaim(price_level: float, start_index: int = 0) -> dict[str, Any] | None:
        for index, point in enumerate(curve[start_index:], start=start_index):
            if float(point["price"]) >= price_level:
                return {**point, "index": index}
        return None

    def held_after(point: dict[str, Any] | None, floor_price: float, bars: int = 3) -> dict[str, Any] | None:
        if not point:
            return None
        index, end = int(point["index"]), int(point["index"]) + bars
        if end >= len(curve):
            return None
        window = curve[index:end + 1]
        if min(float(item["price"]) for item in window) < floor_price:
            return None
        return {"time": curve[end]["time"], "bars_held": bars, "floor_price": round(floor_price, 4),
                "minimum_price": min(float(item["price"]) for item in window)}

    reclaim_start = session_low_index if session_low_pct <= deep_discount_pct else 0
    zero_reclaim = first_reclaim(0.0, reclaim_start) if session_low_pct <= deep_discount_pct else None
    plus_five = first_reclaim(limit_pct * 0.5, reclaim_start)
    limit_reclaim = first_price_reclaim(limit_price * 0.999, reclaim_start) if limit_price else None
    opening_four, opening_eight = first_reclaim(limit_pct * 0.4), first_reclaim(limit_pct * 0.8)
    opening_drive = None
    if (session_low_pct > -3.0 and float(curve[0]["pct_vs_preclose"]) <= 3.0 and opening_four and opening_eight
            and str(opening_four["time"]) <= "0940" and str(opening_eight["time"]) <= "0945"
            and limit_reclaim and str(limit_reclaim["time"]) <= "1000"):
        opening_rows = [row for row in session if str(row["time"]) <= "0940"]
        opening_volume = sum(float(number(row.get("volume_lot", row.get("vol"))) or 0) for row in opening_rows)
        opening_drive = {"open_pct": curve[0]["pct_vs_preclose"], "first_four_pct_time": opening_four["time"],
                         "first_eight_pct_time": opening_eight["time"], "limit_reclaim_time": limit_reclaim["time"],
                         "opening_volume_share": round(opening_volume / total_volume, 5) if total_volume > 0 else None,
                         "interpretation": "开盘累计量与同钟基线应另行确认；封板后的量比尖峰不作为新点火。"}
    daily_low_pct = number(daily.get("low_pct"))
    deep_discount_path = session_low_pct <= -limit_pct * 0.7 or (daily_low_pct is not None and daily_low_pct <= deep_discount_pct)
    deep_discount_stabilization = None
    if deep_discount_path:
        minute_low_price = float(curve[session_low_index]["price"])
        for index, point in enumerate(curve[session_low_index + 1:], start=session_low_index + 1):
            recovery_pct = (float(point["price"]) / minute_low_price - 1) * 100
            if recovery_pct >= 2.5 and float(point["pct_vs_preclose"]) <= 0.5:
                deep_discount_stabilization = {**point, "index": index,
                    "recovery_from_minute_close_low_pct": round(recovery_pct, 4), "confirmation": "price_only_unconfirmed",
                    "interpretation": "只证明低位止跌回收；没有逐笔大单或盘口深度时不能声称大单托举。"}
                break
    reversal_hold = held_after(reversal_impulse, float(reversal_impulse["price"]) * 0.994) if reversal_impulse else None
    zero_hold = held_after(zero_reclaim, previous_close * 0.994) if zero_reclaim else None
    ignition = reversal_impulse or standard_ignition
    ignition_share = None
    if ignition and total_volume > 0:
        index = int(ignition["index"])
        nearby = session[max(0, index - 2):min(len(session), index + 4)]
        ignition_share = sum(float(number(item.get("volume_lot", item.get("vol"))) or 0) for item in nearby) / total_volume
    tags: list[str] = []
    if session_low_pct <= deep_discount_pct and session_high_pct >= limit_reached_pct:
        tags.append("ground_to_sky_reversal")
    elif session_low_pct <= deep_discount_pct and session_high_pct >= 0:
        tags.append("deep_reversal_reclaim")
    elif daily_low_pct is not None and daily_low_pct <= deep_discount_pct and session_low_pct > deep_discount_pct:
        tags.append("intraminute_extreme_not_in_minute_close")
    if deep_discount_stabilization: tags.append("deep_discount_price_stabilization")
    if standard_ignition:
        clock = str(standard_ignition["time"])
        tags.append("opening_drive" if clock <= "1000" else "morning_acceleration" if clock <= "1130" else "midday_relaunch" if "1300" <= clock <= "1400" else "late_acceleration")
    if opening_drive: tags.append("opening_ladder_drive")
    if limit_reclaim: tags.append("limit_reached")
    if session_high_pct - session_low_pct < 0.8: tags.append("one_word_or_near_one_word")
    return {"status": "completed", "minute_rows": len(session), "source": "tencent_free_minute",
            "pattern_tags": tags or ["unclassified_limit_lift"], "session_low": {"time": session_low_time, "pct_vs_preclose": round(session_low_pct, 4)},
            "session_high": {"time": session_high_time, "pct_vs_preclose": round(session_high_pct, 4)},
            "standard_ignition": standard_ignition, "opening_drive": opening_drive,
            "deep_discount_stabilization": deep_discount_stabilization, "deep_reversal_impulse": reversal_impulse,
            "reversal_impulse_hold": reversal_hold, "previous_close_reclaim": zero_reclaim,
            "previous_close_acceptance": zero_hold, "plus_five_reclaim": plus_five, "limit_reclaim": limit_reclaim,
            "post_limit_volume_spike_minutes": sum(1 for point in curve[int(limit_reclaim["index"]) + 1:]
                if limit_reclaim and float(point.get("minute_volume_multiple") or 0) >= 2.5
                and abs(float(point.get("return_3m_pct") or 0)) < 0.2) if limit_reclaim else 0,
            "ignition_six_minute_volume_share": round(ignition_share, 5) if ignition_share is not None else None,
            "curve": curve[:260], "interpretation": "关键点是点时可得的回放标签；地天反转先观察，收复昨收并保持后再人工复核；封板后的静止量比不算二次点火。"}


__all__ = ["intraday_limit_lift_pattern"]
