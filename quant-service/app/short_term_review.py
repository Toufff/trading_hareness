"""Seven-step, evidence-first short-term post-close review.

This is a descriptive projection, not a signal generator.  It turns already
persisted limit-pool events, daily bars, board-flow summaries and LHB events
into a compact next-session checklist.  Missing evidence is represented
explicitly instead of being filled with web commentary or inferred board
membership.
"""

from __future__ import annotations

import json
from collections import Counter
from statistics import mean
from typing import Any


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _body(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("body")
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_rows(rows: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("event_type") or "") == event_type]


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = _body(row)
    # Event persistence retains the provider event under one of these keys;
    # keep this tolerant because AKShare/Tushare raw rows use different names.
    for key in ("payload", "raw", "data"):
        if isinstance(payload.get(key), dict):
            return {**payload, **payload[key]}
    return payload


def _limit_value(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(payload.get(key))
        if value is not None:
            return value
    return None


def _limit_ratio(symbol: str) -> float:
    code = str(symbol or "")[:3]
    if code in {"300", "301", "688", "689"}:
        return 20.0
    if code.startswith(("8", "4")):
        return 30.0
    return 10.0


def _bar_change_pct(row: dict[str, Any], field: str) -> float | None:
    """Return a same-session price change without inferring a missing price."""
    price = _number(row.get(field))
    pre_close = _number(row.get("pre_close"))
    if price is None or pre_close is None or pre_close == 0:
        return None
    return (price / pre_close - 1) * 100


def _market_emotion(limit_ups: list[dict[str, Any]], limit_downs: list[dict[str, Any]], previous: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [_limit_value(_event_payload(row), "涨跌幅", "pct_chg", "pct_change") for row in previous]
    changes = [value for value in changes if value is not None]
    average = mean(changes) if changes else None
    positive = sum(value > 0 for value in changes)
    negative = sum(value < 0 for value in changes)
    premium = positive / len(changes) if changes else None
    down_count = len(limit_downs)
    if not changes and not limit_ups and not limit_downs:
        state = "insufficient"
    elif (average is not None and average < -1.0) or (down_count > len(limit_ups) and down_count >= 3):
        state = "risk_off"
    elif (average is not None and average >= 1.0 and premium >= 0.55) and len(limit_ups) > down_count:
        state = "risk_on"
    else:
        state = "mixed_transition"
    return {
        "state": state,
        "limit_up_count": len(limit_ups),
        "limit_down_count": down_count,
        "previous_limit_count": len(previous),
        "previous_limit_positive_ratio": round(premium, 4) if premium is not None else None,
        "previous_limit_average_change_pct": round(average, 4) if average is not None else None,
        "previous_limit_positive_count": positive,
        "previous_limit_negative_count": negative,
        "evidence_status": "completed" if previous or limit_ups or limit_downs else "missing",
        "interpretation": {
            "risk_on": "昨日涨停溢价和涨停广度支持试错，但仍需板块与竞价确认",
            "risk_off": "昨日强势股亏钱效应扩散，次日不追高，优先观察修复而非接力",
            "mixed_transition": "情绪处于分化/过渡，交易只做有板块共振的少数标的",
            "insufficient": "缺少涨跌停或昨日涨停样本，不能判断短线容错率",
        }[state],
    }


def _ladder(limit_ups: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[int] = Counter()
    symbols_by_height: dict[int, list[dict[str, Any]]] = {}
    for row in limit_ups:
        payload = _event_payload(row)
        value = _limit_value(payload, "连板数", "board_count", "昨日连板数")
        height = int(value) if value is not None and value >= 1 else 1
        counts[height] += 1
        symbols_by_height.setdefault(height, []).append({"symbol": row.get("symbol"), "name": payload.get("名称") or payload.get("name")})
    heights = sorted(counts)
    highest = max(heights) if heights else None
    gaps = [height for height in range(1, highest + 1) if height not in counts] if highest else []
    return {
        "highest_board_count": highest,
        "multi_board_count": sum(count for height, count in counts.items() if height >= 2),
        "distribution": [{"board_count": height, "count": counts[height]} for height in heights],
        "gaps_below_highest": gaps,
        "highest_symbols": symbols_by_height.get(highest, [])[:10] if highest else [],
        "ladder_state": "complete" if heights and not gaps else "fragmented" if heights else "missing",
        "interpretation": "梯队完整" if heights and not gaps else "高度存在但梯队有断层" if heights else "没有可用连板样本",
    }


def _sector_structure(board_summary: dict[str, Any]) -> dict[str, Any]:
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for taxonomy, value in (board_summary or {}).items():
        if not isinstance(value, dict):
            continue
        for direction, target in (("inflow", positive), ("outflow", negative)):
            for item in value.get(direction) or []:
                if not isinstance(item, dict):
                    continue
                stocks = [stock for stock in item.get("top_stocks") or [] if isinstance(stock, dict)]
                limit_like = sum(1 for stock in stocks if (_number(stock.get("pct_change")) or 0) >= _limit_ratio(str(stock.get("symbol") or "")) - 0.3)
                rising = sum(1 for stock in stocks if (_number(stock.get("pct_change")) or 0) > 0)
                if limit_like >= 3:
                    structure_state = "limit_cluster"
                elif limit_like >= 1 and rising >= 3:
                    structure_state = "leader_with_followers"
                elif limit_like >= 1:
                    structure_state = "isolated_leader"
                else:
                    structure_state = "unconfirmed"
                target.append({
                    "taxonomy_key": taxonomy,
                    "sector_key": item.get("sector_key"),
                    "label": item.get("label"),
                    "net_inflow": item.get("net_inflow"),
                    "change_pct": item.get("change_pct"),
                    "top_stock_count": len(stocks),
                    "limit_like_top_stock_count": limit_like,
                    "rising_top_stock_count": rising,
                    "structure_state": structure_state,
                    "mapped_members": item.get("mapped_members", 0),
                    "quoted_members": item.get("quoted_members", 0),
                })
    positive.sort(key=lambda item: _number(item.get("net_inflow")) or float("-inf"), reverse=True)
    negative.sort(key=lambda item: _number(item.get("net_inflow")) or float("inf"))
    complete = [item for item in positive if int(item.get("mapped_members") or 0) > 0 and int(item.get("quoted_members") or 0) > 0]
    return {
        "inflow_leaders": positive[:10],
        "outflow_leaders": negative[:10],
        "candidate_mainlines": [item for item in complete if item["limit_like_top_stock_count"] >= 3][:10],
        "complete_board_count": len(complete),
        "evidence_status": "completed" if positive or negative else "missing",
        "coverage_note": "板块强度只使用已保存的成分/报价字段；未映射板块不推断涨停数量。候选主线要求至少三只涨停样本；中军身份需要市值/机构持仓证据，当前不猜测。",
    }


def _capital_and_loss(
    daily_rows: list[dict[str, Any]],
    lhb_rows: list[dict[str, Any]],
    tushare_lhb_context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ranked = sorted(daily_rows, key=lambda row: _number(row.get("amount")) or float("-inf"), reverse=True)
    top20 = ranked[:20]
    daily_symbol_count = len(daily_rows)
    market_amount = sum(max(0.0, _number(row.get("amount")) or 0.0) for row in daily_rows)
    top20_amount = sum(max(0.0, _number(row.get("amount")) or 0.0) for row in top20)
    raw_top20_share = top20_amount / market_amount if market_amount else None
    # This is a data-quality bound, not a strategy threshold.  When only 20
    # rows account for half of an ostensibly all-A total, the saved amount
    # field is incomplete or has an incompatible unit and cannot support a
    # market-wide concentration conclusion.
    full_market_daily = daily_symbol_count >= 3000 and raw_top20_share is not None and raw_top20_share <= 0.5
    changes = [_number(row.get("pct_chg", row.get("pct_change"))) for row in top20]
    changes = [value for value in changes if value is not None]
    lhb_net: list[float] = []
    for row in lhb_rows:
        payload = _event_payload(row)
        value = _limit_value(payload, "龙虎榜净买额", "net_amount", "net_buy_amount", "净买额")
        if value is not None:
            lhb_net.append(value)
    institution_records = sum(int(item.get("institution_records") or 0) for item in tushare_lhb_context.values())
    institution_net_buy = sum(_number(item.get("institution_net_buy")) or 0.0 for item in tushare_lhb_context.values())
    capital = {
        "top_amount_count": len(top20),
        "top_amount_advancers": sum(value > 0 for value in changes),
        "top_amount_decliners": sum(value < 0 for value in changes),
        "top_amount_average_change_pct": round(mean(changes), 4) if changes else None,
        "daily_symbol_count": daily_symbol_count,
        "top_amount_evidence_status": "completed" if full_market_daily else "partial",
        "top_amount_quality_flags": [] if full_market_daily else [
            "insufficient_all_a_daily_coverage" if daily_symbol_count < 3000 else "amount_distribution_anomaly"
        ],
        "market_amount": round(market_amount, 4) if market_amount and full_market_daily else None,
        "top20_amount_share": round(raw_top20_share, 4) if raw_top20_share is not None and full_market_daily else None,
        "top_amount_symbols": [{"symbol": row.get("symbol"), "name": row.get("name"), "amount": row.get("amount"), "pct_change": row.get("pct_chg", row.get("pct_change"))} for row in top20],
        "lhb_stock_count": len(lhb_rows),
        "lhb_net_amount_sum": round(sum(lhb_net), 4) if lhb_net else None,
        "lhb_positive_net_count": sum(value > 0 for value in lhb_net),
        "lhb_negative_net_count": sum(value < 0 for value in lhb_net),
        "tushare_lhb_symbol_count": len(tushare_lhb_context),
        "tushare_institution_records": institution_records,
        "tushare_institution_net_buy": round(institution_net_buy, 4) if institution_records else None,
        "lhb_seat_evidence_status": "completed" if tushare_lhb_context else "missing",
        "coverage_note": "成交额前20只有在至少 3,000 只本地 A 股日线可用、且前20成交额占比通过分布合理性检查时才代表全市场；否则只保留局部研究样本，不解读集中度。",
    }
    return capital


def _loss_effect(
    daily_rows: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    limit_opens: list[dict[str, Any]],
    capital: dict[str, Any],
) -> dict[str, Any]:
    largest_losses = sorted(
        [dict(row) for row in daily_rows if (_number(row.get("pct_chg", row.get("pct_change"))) or 0) < 0],
        key=lambda row: _number(row.get("pct_chg", row.get("pct_change"))) or 0,
    )[:10]
    previous_deep_losses: list[dict[str, Any]] = []
    for row in previous:
        payload = _event_payload(row)
        change = _limit_value(payload, "涨跌幅", "pct_chg", "pct_change")
        if change is not None and change <= -5:
            previous_deep_losses.append({"symbol": row.get("symbol"), "name": payload.get("名称") or payload.get("name"), "pct_change": round(change, 4)})
    intraday_reversals: list[dict[str, Any]] = []
    for row in daily_rows:
        high_change = _bar_change_pct(row, "high")
        close_change = _number(row.get("pct_chg", row.get("pct_change")))
        if high_change is None or close_change is None:
            continue
        if high_change >= 5 and high_change - close_change >= 7 and close_change <= 0:
            intraday_reversals.append({"symbol": row.get("symbol"), "name": row.get("name"),
                                       "high_change_pct": round(high_change, 4), "close_change_pct": round(close_change, 4)})
    intraday_reversals.sort(key=lambda item: item["high_change_pct"] - item["close_change_pct"], reverse=True)
    loss = {
        "largest_losses": [{"symbol": row.get("symbol"), "name": row.get("name"), "pct_change": row.get("pct_chg", row.get("pct_change"))} for row in largest_losses],
        "negative_daily_count": sum((_number(row.get("pct_chg", row.get("pct_change"))) or 0) < 0 for row in daily_rows),
        "previous_limit_deep_loss_count": len(previous_deep_losses),
        "previous_limit_deep_losses": previous_deep_losses[:10],
        "limit_open_count": len(limit_opens),
        "intraday_reversal_count": len(intraday_reversals),
        "intraday_reversals": intraday_reversals[:10],
        "risk_flags": [],
    }
    if loss["negative_daily_count"] and loss["negative_daily_count"] > max(1, len(daily_rows) * 0.6):
        loss["risk_flags"].append("broad_loss_effect")
    if capital["top_amount_decliners"] > capital["top_amount_advancers"]:
        loss["risk_flags"].append("large_capital_leaders_weak")
    if len(previous_deep_losses) >= max(2, round(len(previous) * 0.25)):
        loss["risk_flags"].append("previous_limit_deep_loss_cluster")
    if len(limit_opens) >= 3:
        loss["risk_flags"].append("limit_open_supply_pressure")
    if len(intraday_reversals) >= max(5, round(len(daily_rows) * 0.005)):
        loss["risk_flags"].append("intraday_reversal_cluster")
    return loss


def _watch_flags(
    limit_ups: list[dict[str, Any]],
    previous: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    loss: dict[str, Any],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for row in limit_ups:
        payload = _event_payload(row)
        board_count = _limit_value(payload, "连板数", "board_count") or 1
        flag_type = "consecutive_limit" if board_count >= 2 else "first_board"
        flags.append({"symbol": row.get("symbol"), "name": payload.get("名称") or payload.get("name"), "type": flag_type, "board_count": int(board_count), "reason": "涨停池/连板梯队风向标，次日需竞价和板块承接确认",
                      "next_session_trigger": "竞价不显著弱于同梯队，开盘后板块保持正向承接", "invalidation": "快速跌破前收且同梯队/板块同步走弱"})
    for row in previous:
        payload = _event_payload(row)
        change = _limit_value(payload, "涨跌幅", "pct_chg", "pct_change")
        if change is not None and change > 2:
            flags.append({"symbol": row.get("symbol"), "name": payload.get("名称") or payload.get("name"), "type": "previous_limit_repair", "board_count": _limit_value(payload, "昨日连板数", "board_count"), "reason": "昨日强势样本仍有正溢价，作为修复观察而非直接追涨",
                          "next_session_trigger": "开盘后保持前收上方并出现二次承接", "invalidation": "低开后无法收复前收，且昨日涨停样本转弱"})
    if "broad_loss_effect" in set(loss.get("risk_flags") or []):
        resilient = sorted(
            [row for row in daily_rows if (_number(row.get("pct_chg", row.get("pct_change"))) or 0) >= 3],
            key=lambda row: _number(row.get("pct_chg", row.get("pct_change"))) or 0,
            reverse=True,
        )
        for row in resilient[:3]:
            flags.append({"symbol": row.get("symbol"), "name": row.get("name"), "type": "resilient_in_broad_pullback", "board_count": None,
                          "reason": "全市场亏钱效应下仍显著走强，作为相对强度风向标",
                          "next_session_trigger": "竞价和开盘维持相对市场强度，且所属板块未转弱", "invalidation": "相对强度消失并跌回前收下方"})
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in flags:
        symbol = str(item.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        unique.append(item)
        seen.add(symbol)
        if len(unique) >= 8:
            break
    return unique


def build_short_term_review(
    *,
    event_rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    board_summary: dict[str, Any],
    tushare_lhb_context: dict[str, dict[str, Any]] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    limit_ups = _event_rows(event_rows, "limit_up_pool")
    limit_downs = _event_rows(event_rows, "limit_down_pool")
    previous = _event_rows(event_rows, "previous_limit_pool")
    limit_opens = _event_rows(event_rows, "limit_open_pool")
    lhb_rows = _event_rows(event_rows, "lhb_event")
    emotion = _market_emotion(limit_ups, limit_downs, previous)
    ladder = _ladder(limit_ups)
    sectors = _sector_structure(board_summary)
    capital = _capital_and_loss(daily_rows, lhb_rows, tushare_lhb_context or {})
    loss = _loss_effect(daily_rows, previous, limit_opens, capital)
    wind_flags = _watch_flags(limit_ups, previous, daily_rows, loss)
    if emotion["state"] == "risk_off":
        participation = "观望/只做已有底仓的风险管理，等待亏钱效应收敛"
        triggers = ["昨日涨停整体转正", "最高板不再断层", "主线板块至少出现龙头与中军同步"]
    elif emotion["state"] == "risk_on":
        participation = "允许小仓位试错，但只参与板块共振和竞价/开盘承接一致的标的"
        triggers = ["竞价不弱于预案", "开盘后量价与板块方向同步", "两次扫描确认而非单帧脉冲"]
    else:
        participation = "轻仓、快进快出，优先修复型和有精确板块证据的标的"
        triggers = ["板块净流入持续而非单点跳变", "个股收复 VWAP/前收并保持承接", "风险标的没有扩散"]
    return {
        "status": "completed" if event_rows or daily_rows or board_summary else "partial",
        "observed_at": observed_at,
        "methodology": "short-term-review-v2",
        "market_emotion": emotion,
        "ladder": ladder,
        "sector_structure": sectors,
        "capital_and_lhb": capital,
        "loss_effect": loss,
        "wind_flags": wind_flags,
        "next_session_plan": {
            "participation": participation,
            "triggers": triggers,
            "invalidations": ["集合竞价/开盘快速跌破关键价位", "板块资金由流入转持续流出", "昨日强势股批量低开走弱"],
            "symbols": [item["symbol"] for item in wind_flags],
            "symbol_plans": [{key: item.get(key) for key in ("symbol", "name", "type", "reason", "next_session_trigger", "invalidation")} for item in wind_flags],
            "decision_eligible": False,
        },
        "notice": "七步复盘是证据整理和次日预案，不是自动交易信号；缺失数据不会被网络观点补齐。",
    }


__all__ = ["build_short_term_review"]
