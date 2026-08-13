"""Pure intraday price/volume and peer-breadth features."""

from __future__ import annotations

import math
import re
from statistics import mean, median
from typing import Any, Callable


def minute_features(rows: list[dict[str, Any]], *, lookback: int = 20,
                    source: str = "tencent_free",
                    number: Callable[[Any], float | None]) -> dict[str, Any] | None:
    """Build a causal price/volume burst feature from normalized minute rows."""
    if len(rows) < 6:
        return None
    current = rows[-1]
    price = number(current.get("close"))
    if price is None or price <= 0:
        return None

    def past_return(offset: int) -> float | None:
        previous = number(rows[-1 - offset].get("close")) if len(rows) > offset else None
        return round((price / previous - 1) * 100, 4) if previous and previous > 0 else None

    prior_volumes = [number(row.get("volume_lot", row.get("vol"))) for row in rows[-1 - lookback:-1]]
    valid_prior = [float(value) for value in prior_volumes if value is not None and value > 0]
    baseline = median(valid_prior) if len(valid_prior) >= 5 else None
    current_volume = number(current.get("volume_lot", current.get("vol")))
    vwap = number(current.get("vwap"))
    session_prices = [number(row.get("close")) for row in rows]
    valid_session_prices = [float(value) for value in session_prices if value is not None and value > 0]
    session_low = min(valid_session_prices) if valid_session_prices else None
    session_high = max(valid_session_prices) if valid_session_prices else None
    opening_price = number(rows[0].get("close"))
    prior_session_prices = valid_session_prices[:-1]
    prior_session_high = max(prior_session_prices) if prior_session_prices else None
    window = rows[-min(len(rows), 30):]
    window_prices = [number(row.get("close")) for row in window]
    window_volumes = [number(row.get("volume_lot", row.get("vol"))) for row in window]
    paired = [(float(close), math.log(float(volume) + 1.0)) for close, volume in zip(window_prices, window_volumes, strict=True)
              if close is not None and close > 0 and volume is not None and volume >= 0]
    if len(paired) >= 8:
        prices_for_corr, volumes_for_corr = zip(*paired, strict=True)
        price_mean, volume_mean = mean(prices_for_corr), mean(volumes_for_corr)
        covariance = sum((left - price_mean) * (right - volume_mean) for left, right in paired)
        price_variance = sum((left - price_mean) ** 2 for left in prices_for_corr)
        volume_variance = sum((right - volume_mean) ** 2 for right in volumes_for_corr)
        price_volume_corr = round(covariance / math.sqrt(price_variance * volume_variance), 6) if price_variance and volume_variance else None
    else:
        price_volume_corr = None
    window_amount = sum(float(number(row.get("amount")) or 0) for row in window)
    window_volume = sum(float(number(row.get("volume_lot", row.get("vol"))) or 0) * 100 for row in window)
    window_vwap = window_amount / window_volume if window_amount > 0 and window_volume > 0 else None
    smart_rows = [(abs(math.log(float(row.get("close")) / float(previous.get("close")))) / math.sqrt(max(float(row.get("volume_lot", row.get("vol")) or 0), 1.0)), row)
                  for previous, row in zip(window[:-1], window[1:], strict=True)
                  if number(row.get("close")) and number(previous.get("close")) and number(row.get("volume_lot", row.get("vol"))) is not None]
    smart_rows.sort(key=lambda item: item[0], reverse=True)
    target_smart_volume = window_volume * 0.2
    selected_smart_rows: list[dict[str, Any]] = []
    selected_smart_volume = 0.0
    for _, row in smart_rows:
        selected_smart_rows.append(row)
        selected_smart_volume += float(number(row.get("volume_lot", row.get("vol"))) or 0) * 100
        if selected_smart_volume >= target_smart_volume:
            break
    smart_amount = sum(float(number(row.get("amount")) or 0) for row in selected_smart_rows)
    smart_volume = sum(float(number(row.get("volume_lot", row.get("vol"))) or 0) * 100 for row in selected_smart_rows)
    smart_vwap = smart_amount / smart_volume if smart_amount > 0 and smart_volume > 0 else None
    q_smart_money = round(smart_vwap / window_vwap, 6) if smart_vwap and window_vwap and window_vwap > 0 else None
    return {
        "time": current.get("time"), "price": price, "is_complete": bool(current.get("is_complete")),
        "return_1m_pct": past_return(1), "return_3m_pct": past_return(3), "return_5m_pct": past_return(5),
        "minute_volume_lot": current_volume, "minute_amount": number(current.get("amount")),
        "volume_baseline_lot": round(baseline, 2) if baseline is not None else None,
        "minute_volume_multiple": round(current_volume / baseline, 4) if current_volume is not None and baseline else None,
        "vwap": vwap, "above_vwap_pct": round((price / vwap - 1) * 100, 4) if vwap and vwap > 0 else None,
        "session_low_price": session_low, "session_high_price": session_high,
        "recovery_from_session_low_pct": round((price / session_low - 1) * 100, 4) if session_low else None,
        "return_from_open_pct": round((price / opening_price - 1) * 100, 4) if opening_price and opening_price > 0 else None,
        "breakout_above_prior_high_pct": round((price / prior_session_high - 1) * 100, 4)
        if prior_session_high and prior_session_high > 0 else None,
        "session_range_position": round((price - session_low) / (session_high - session_low), 4)
        if session_low is not None and session_high is not None and session_high > session_low else 0.0,
        "price_log_volume_corr_30m": price_volume_corr,
        "smart_money_q_30m": q_smart_money,
        "smart_money_vwap_30m": round(smart_vwap, 6) if smart_vwap else None,
        "smart_money_window_vwap_30m": round(window_vwap, 6) if window_vwap else None,
        "smart_money_selected_volume_share_30m": round(smart_volume / window_volume, 6) if smart_volume and window_volume else None,
        "source": source,
    }


def peer_context(peer_symbols: list[str], features: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Measure same-minute breadth without allowing the target into its peers."""
    peers = [{"symbol": symbol, **features[symbol]} for symbol in peer_symbols if symbol in features]
    confirming = [item for item in peers
                  if float(item.get("return_1m_pct") or 0) >= 0.5
                  and float(item.get("return_3m_pct") or 0) >= 0.8
                  and float(item.get("minute_volume_multiple") or 0) >= 1.8]
    return {
        "requested_peer_count": len(peer_symbols), "available_peer_count": len(peers),
        "confirming_peer_count": len(confirming),
        "confirming_breadth": round(len(confirming) / len(peers), 4) if peers else 0,
        "confirming_symbols": [item["symbol"] for item in confirming], "peers": peers,
    }


def strategy_session_rows(rows: list[dict[str, Any]], *, number: Callable[[Any], float | None]) -> list[dict[str, Any]]:
    """Keep continuous-auction minutes and one value per minute."""
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        minute = str(row.get("time") or "").replace(":", "")[:4]
        if not re.fullmatch(r"\d{4}", minute):
            continue
        if not ("0930" <= minute <= "1130" or "1300" <= minute <= "1500"):
            continue
        price = number(row.get("close"))
        if price is None or price <= 0:
            continue
        selected[minute] = {**row, "time": minute, "close": price}
    return [selected[key] for key in sorted(selected)]


__all__ = ["minute_features", "peer_context", "strategy_session_rows"]
