"""Advanced short-term setups evaluated from strict OHLC history.

Full-market order-size snapshots contain closes but no highs/lows.  These
rules therefore accept separately fetched, timestamped Longhu OHLC history;
missing history produces a lane-level data gap instead of a fabricated setup.
"""
from __future__ import annotations

from math import isfinite
from statistics import fmean, median, pstdev
from typing import Any
from ..short_term_liquidity import rerank
from .price_volume import PriceVolumeSettings


ADVANCED_LANES = (
    ("contraction", "波动收缩突破", "20–60日波动与成交额先收缩，区分接近压力与收盘越过前10日真实高点"),
    ("rotation", "板块轮动初动", "板块广度、资金和中位个股连续改善，寻找尚未过度加速的先行股"),
    ("reclaim", "恐慌回收", "排除已核验利空后，急跌或假跌破被价格收复且抛压衰减"),
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def prefilter_symbols(rows: list[dict], sessions: list[str], limit: int = 72) -> list[str]:
    """Cheap close-only prefilter; it never constitutes an advanced signal."""
    wanted = {day.replace("-", "") for day in sessions[-11:]}
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        code = str(row.get("symbol", "")).split(".", 1)[0]
        name = str(row.get("name") or "").upper()
        if not (len(code) == 6 and code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))):
            continue
        if name.startswith(("ST", "*ST")) or "退" in name:
            continue
        if str(row.get("trade_date", "")).replace("-", "") in wanted:
            grouped.setdefault(str(row.get("symbol")), []).append(row)
    ranked = []
    for symbol, values in grouped.items():
        values.sort(key=lambda row: str(row.get("trade_date")))
        if len(values) < 11:
            continue
        closes = [_number(row.get("close")) for row in values[-11:]]
        amounts = [_number(row.get("amount")) for row in values[-11:]]
        if any(value is None or value <= 0 for value in closes + amounts):
            continue
        p = [float(value) for value in closes]
        a = [float(value) for value in amounts]
        range10 = (max(p[-10:]) / min(p[-10:]) - 1) * 100
        latest_change = (p[-1] / p[-2] - 1) * 100
        previous_change = (p[-2] / p[-3] - 1) * 100
        contraction_interest = max(0.0, 8.0 - range10) + max(0.0, 1.0 - a[-2] / fmean(a[-7:-2])) * 5
        reclaim_interest = max(0.0, -previous_change - 2.5) + max(0.0, latest_change) + max(0.0, 8.0 - range10) * 0.25
        interest = max(contraction_interest, reclaim_interest)
        # The enrichment queue must not spend limited OHLC slots on stocks
        # the downstream daily activity floor will immediately discard.
        turnover = _number(values[-1].get('turnover_rate'))
        if interest <= 0 or a[-1] < 300_000_000 or turnover is None or not 1.5 <= turnover <= 35:
            continue
        ranked.append(dict(symbol=symbol, raw_score=interest, state='watch',
                           metrics=dict(amount=a[-1],turnover=turnover,
                                        flow_series=[dict(amount=x) for x in a[-5:]])))
    rerank(ranked)
    ranked.sort(key=lambda item: (-item['rank_score'], item['symbol']))
    return [item['symbol'] for item in ranked[:max(0,limit)]]


def ohlc_features(rows: list[dict], as_of_date: str,
                  settings: PriceVolumeSettings = PriceVolumeSettings()) -> dict | None:
    usable = []
    for row in rows:
        if str(row.get("date", "")) > as_of_date:
            continue
        values = [_number(row.get(key)) for key in ("open", "high", "low", "close", "amount")]
        if any(value is None or value <= 0 for value in values):
            continue
        o, h, l, c, _amount = values
        if not l <= min(o, c) <= max(o, c) <= h:
            continue
        usable.append({**row, **dict(zip(("open", "high", "low", "close", "amount"), values))})
    usable.sort(key=lambda row: row["date"])
    if (len(usable) < 40 or usable[-1]['date'] != as_of_date
            or len({r['date'] for r in usable}) != len(usable)):
        return None
    usable = usable[-60:]
    closes = [float(row["close"]) for row in usable]
    highs = [float(row["high"]) for row in usable]
    lows = [float(row["low"]) for row in usable]
    amounts = [float(row["amount"]) for row in usable]
    true_ranges = []
    for index, row in enumerate(usable):
        previous = closes[index - 1] if index else closes[index]
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - previous), abs(lows[index] - previous)))
    bandwidths = []
    for index in range(19, len(closes)):
        sample = closes[index - 19:index + 1]
        center = fmean(sample)
        bandwidths.append((4 * pstdev(sample) / center) * 100 if center else 0.0)
    previous_change = (closes[-2] / closes[-3] - 1) * 100
    latest_change = (closes[-1] / closes[-2] - 1) * 100
    lost = closes[-3] - closes[-2]
    recovery_fraction = (closes[-1]-closes[-2])/lost if lost > 0 else None
    reference_low = min(lows[-12:-2])
    false_break_recovered = closes[-2] < reference_low < closes[-1]
    close_location = ((closes[-1]-lows[-1])/(highs[-1]-lows[-1])
                      if highs[-1] > lows[-1] else None)
    meaningful_recovery = (
        latest_change >= settings.reclaim_min_change_pct
        and close_location is not None and close_location >= settings.reclaim_min_close_location
        and (false_break_recovered or (recovery_fraction is not None
             and recovery_fraction >= settings.reclaim_min_recovery_fraction))
    )
    return {
        "history_rows": len(usable), "close": closes[-1], "latest_change": latest_change,
        "previous_change": previous_change, "prior10_high": max(highs[-11:-1]),
        "prior10_low": min(lows[-11:-1]), "latest_high": highs[-1], "latest_low": lows[-1],
        "previous_high": highs[-2], "previous_low": lows[-2],
        "first_close_breakout": closes[-1] > max(highs[-11:-1]),
        "breakout_scope": "首次收盘越过本次前10日真实高点；不是长期首次突破或盘中触及次数",
        "recovery_fraction": recovery_fraction, "close_location": close_location,
        "false_break_recovered": false_break_recovered,
        "meaningful_recovery": meaningful_recovery,
        "reclaim_reference": reference_low if false_break_recovered else closes[-3],
        "panic_low": lows[-2],
        "atr14_pct": fmean(true_ranges[-14:]) / closes[-1] * 100,
        "range10_pct": (max(highs[-10:]) / min(lows[-10:]) - 1) * 100,
        "tr_contract_ratio": fmean(true_ranges[-6:-1]) / fmean(true_ranges[-16:-6]),
        "amount_contract_ratio": fmean(amounts[-6:-1]) / fmean(amounts[-16:-6]),
        "amount_expansion": amounts[-1] / fmean(amounts[-6:-1]),
        "bandwidth20": bandwidths[-1],
        "bandwidth_percentile": sum(value <= bandwidths[-1] for value in bandwidths) / len(bandwidths),
        "panic_break": previous_change <= -4.0 or closes[-2] < min(lows[-12:-2]),
        "reclaimed_previous_high": closes[-1] > highs[-2],
        "reclaimed_prior_low": closes[-1] > min(lows[-12:-2]),
        "bars": usable,
    }


def sector_rotation_metrics(features: dict[str, dict], latest: dict[str, dict], market_r10: float) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = {}
    for symbol, row in features.items():
        grouped.setdefault(str(latest[symbol].get("plate_id") or "unknown"), []).append(row)
    result: dict[str, dict] = {}
    for sector, members in grouped.items():
        if sector == "unknown" or len(members) < 5:
            continue
        breadth = []
        medians = []
        net = []
        for index in range(10):
            changes = [member["return_series"][index]["change"] for member in members]
            breadth.append(sum(change > 0 for change in changes) / len(changes))
            medians.append(median(changes))
            net.append(sum(member["flow_series"][index + 1]["net"] for member in members))
        prior_breadth = fmean(breadth[-6:-3])
        recent_breadth = fmean(breadth[-3:])
        stable_leaders = sum(member["return_10d"] >= max(5.0, market_r10 + 4.0) for member in members)
        result[sector] = {
            "members": len(members), "latest_breadth": breadth[-1],
            "breadth_acceleration": recent_breadth - prior_breadth,
            "recent_breadth": recent_breadth, "prior_breadth": prior_breadth,
            "median_change_3d": fmean(medians[-3:]), "flow_3d": sum(net[-3:]),
            "stable_leaders": stable_leaders,
        }
    return result


def evaluate(
    key: str, base: dict, *, ohlc: dict | None, sector_rotation: dict | None,
    events: list[dict], verified_negative: bool,
) -> tuple[bool, float, str, dict]:
    if key == "contraction":
        if not ohlc:
            return False, 0.0, "缺少至少40根严格OHLC日线", {"data_gap": "strict_ohlc_40"}
        raw_match = (
            ohlc["tr_contract_ratio"] <= 0.78 and ohlc["amount_contract_ratio"] <= 0.88
            and ohlc["bandwidth_percentile"] <= 0.35
            and ohlc["close"] >= ohlc["prior10_high"] * 0.995
            and ohlc["amount_expansion"] >= 1.20
            and 0.8 <= ohlc["latest_change"]
            and sector_rotation is not None and sector_rotation["latest_breadth"] >= 0.50
        )
        score = (1 - ohlc["tr_contract_ratio"]) * 10 + min(ohlc["amount_expansion"], 3) * 3 + sector_rotation.get("latest_breadth", 0) * 4 if sector_rotation else 0
        matched = raw_match and ohlc['first_close_breakout']
        reason = (f"近5日真实波幅/此前10日为{ohlc['tr_contract_ratio']:.2f}，成交额收缩比{ohlc['amount_contract_ratio']:.2f}；"
                  f"{'收盘越过' if ohlc['first_close_breakout'] else '收盘接近但未越过'}前10日真实高点{ohlc['prior10_high']:.2f}，成交额放大{ohlc['amount_expansion']:.2f}倍")
        return matched, score, reason, {**ohlc, 'raw_match': raw_match}
    if key == "rotation":
        if not sector_rotation:
            return False, 0.0, "缺少可比较的行业连续广度", {"data_gap": "sector_history"}
        matched = (
            sector_rotation["recent_breadth"] >= 0.52
            and sector_rotation["breadth_acceleration"] >= 0.10
            and sector_rotation["median_change_3d"] > 0
            and sector_rotation["flow_3d"] > 0
            and sector_rotation["stable_leaders"] >= 1
            and 0 <= base["change_pct"] < 7 and base["amount_multiple"] >= 0.9
        )
        score = sector_rotation["breadth_acceleration"] * 10 + sector_rotation["recent_breadth"] * 4 + min(base["amount_multiple"], 2)
        reason = (f"行业近3日上涨广度{sector_rotation['recent_breadth']:.0%}，较此前3日改善"
                  f"{sector_rotation['breadth_acceleration']:+.0%}，行业3日资金合计为正且有稳定先行股")
        return matched, score, reason, sector_rotation
    if not ohlc:
        return False, 0.0, "缺少至少40根严格OHLC日线", {"data_gap": "strict_ohlc_40"}
    negative = verified_negative or any(event.get("impact_direction") == "negative" for event in events)
    raw_match = (
        not negative and ohlc["panic_break"] and ohlc["reclaimed_prior_low"]
        and ohlc["latest_change"] > 0 and ohlc["amount_expansion"] <= 1.8
        and sector_rotation is not None and sector_rotation["latest_breadth"] >= 0.45
    )
    matched = raw_match and ohlc['meaningful_recovery']
    score = max(0.0, -ohlc["previous_change"]) + max(0.0, ohlc["latest_change"]) + max(0.0, 1.8 - ohlc["amount_expansion"])
    recovery_text = (f"回收前日收盘跌幅的{ohlc['recovery_fraction']:.0%}"
                     if ohlc['recovery_fraction'] is not None else '前日非收盘下跌，不计算跌幅回收比例')
    reason = (f"前一交易日{ohlc['previous_change']:+.1f}%，最新收盘{ohlc['latest_change']:+.1f}%，{recovery_text}；"
              f"{'满足日线修复假设' if ohlc['meaningful_recovery'] else '仅弱反弹，修复条件不足'}；"
              f"成交额为近5日均值{ohlc['amount_expansion']:.2f}倍；当前已核验事件中{'存在' if negative else '未见'}负面事件，不等于已排除全部利空")
    return matched, score, reason, {**ohlc, 'raw_match': raw_match}


__all__ = ["ADVANCED_LANES", "evaluate", "ohlc_features", "prefilter_symbols", "sector_rotation_metrics"]
