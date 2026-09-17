"""Pure point-in-time short-term discovery, not a profitability model."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from math import isfinite
from statistics import fmean, median, pstdev
from typing import Any

from . import VERSION
from .advanced_strategies import ADVANCED_LANES, evaluate as evaluate_advanced, ohlc_features, sector_rotation_metrics
from .conditions import watch_conditions
from .discovery import execution_context, observation_list
from .regime import classify as classify_regime, route as route_regime
from .risk import risk_envelope
from .price_volume import PriceVolumeSettings, assess as assess_price_volume
from .event_time import resolve_cutoff, event_unavailable_reason
from ..short_term_liquidity import rerank
from .accumulation_rules import evaluate as evaluate_accumulation
from ..ranking_factors import FactorSpec, validate as validate_factors, build_contexts, apply_factors


@dataclass(frozen=True)
class Settings:
    ranking_factors: tuple[FactorSpec, ...] = ()
    price_volume: PriceVolumeSettings = PriceVolumeSettings()
    liquidity_rank_weight: float = 0.40
    liquidity_target_amount: float = 1_000_000_000
    minimum_amount: float = 300_000_000
    minimum_turnover: float = 1.5
    maximum_turnover: float = 35
    expansion_multiple: float = 1.3
    pullback_multiple: float = 1.05
    top_per_lane: int = 5
    maximum_per_industry: int = 2
    minimum_universe: int = 1000
    minimum_history_coverage: float = 0.95
    crowded_return10: float = 40
    crowded_amount_multiple: float = 4


BASE_LANES = (
    ("accumulation", "潜伏观察", "多日合计流入为正、收盘区间收窄，优先已有活跃迹象的股票"),
    ("expansion", "放量启动", "突破前期收盘平台、成交额放大，板块与个股同步转强"),
    ("pullback", "强势回踩", "此前强势且结构回撤；分别核验回调段成交额收缩与未来止跌承接"),
    ("trend", "主线趋势", "近十个交易日相对强势、板块广度较好；不要求便宜或横盘"),
    ("event", "事件机会", "已核验公司公告或事件与个股对应，再看市场是否开始交易"),
    ("relay", "情绪接力", "当日涨停且成交活跃、板块有同伴；涨停不代表次日可成交"),
)
LANES = (*BASE_LANES, *ADVANCED_LANES)


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def mainboard(row: dict) -> bool:
    code = str(row.get("symbol", "")).split(".")[0]
    name = str(row.get("name") or "").upper()
    return (len(code) == 6 and code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))
            and not name.startswith(("ST", "*ST")) and "退" not in name)


def features(bars: list[dict], sessions: list[str]) -> dict | None:
    """No missing days, mixed definitions, fabricated OHLC or forward filling."""
    by_day = {str(r["trade_date"]).replace("-", ""): r for r in bars}
    keys = [day.replace("-", "") for day in sessions[-11:]]
    if len(keys) < 11 or any(key not in by_day for key in keys):
        return None
    rows = [by_day[key] for key in keys]
    for r in rows:
        if any(number(r.get(k)) is None for k in ("close", "amount", "main_net", "pct_chg")):
            return None
        if number(r["close"]) <= 0 or number(r["amount"]) <= 0:
            return None
    p = [float(r["close"]) for r in rows]
    # Historical prices here are unadjusted. A material ex-right discontinuity
    # cannot masquerade as a pullback or breakout.
    if any(abs((p[i] / p[i-1] - 1) * 100 - float(rows[i]["pct_chg"])) > 0.8 for i in range(1, len(p))):
        return None
    a = [float(r["amount"]) for r in rows]
    flows = [float(r["main_net"]) for r in rows]
    returns = [(p[i] / p[i-1] - 1) * 100 for i in range(1, len(p))]
    latest = rows[-1]
    return {
        "close": p[-1], "change_pct": returns[-1], "amount": a[-1],
        "turnover": number(latest.get("turnover_rate")),
        "return_5d": (p[-1] / p[-6] - 1) * 100,
        "return_10d": (p[-1] / p[0] - 1) * 100,
        "prior_gain": (max(p[:-1]) / p[0] - 1) * 100,
        "drawdown": (p[-1] / max(p) - 1) * 100,
        "amount_multiple": a[-1] / fmean(a[-6:-1]),
        "ma5": fmean(p[-5:]), "ma10": fmean(p[-10:]),
        "prior_high": max(p[-6:-1]), "recent_low": min(p[-5:]),
        "range5": (max(p[-5:]) / min(p[-5:]) - 1) * 100,
        "range10": (max(p[-10:]) / min(p[-10:]) - 1) * 100,
        "volatility": pstdev(returns), "net5": sum(flows[-5:]), "net10": sum(flows[-10:]),
        "positive_days5": sum(f > 0 for f in flows[-5:]),
        "net5_amount_pct": sum(flows[-5:]) / sum(a[-5:]) * 100,
        "net10_amount_pct": sum(flows[-10:]) / sum(a[-10:]) * 100,
        "flow_series": [{"date": sessions[-11+i], "net": flows[i], "close": p[i], "amount": a[i]} for i in range(11)],
        "return_series": [{"date": sessions[-10+i], "change": returns[i]} for i in range(10)],
    }


def whole_sector_overview(sectors: dict[str, dict], rotation: dict[str, dict],
                          labels: dict[str, str], market_r10: float) -> dict[str, dict]:
    """Aggregate every listed member of each sector, not only today's candidates.

    A recommendation has to judge the whole industry's trend and volume/price,
    which the per-stock candidate rows cannot show.  ``relative_strength`` is a
    reference label describing where the sector sits against the market median,
    never a score, a ranking or an expected return.  Sectors with fewer than
    five complete members, and the unknown bucket, are left out: their medians
    would not describe an industry.
    """
    overview: dict[str, dict] = {}
    for key, stats in sectors.items():
        if key == "unknown" or stats["members"] < 5:
            continue
        rotated = rotation.get(key) or {}
        acceleration, flow_3d = rotated.get("breadth_acceleration"), rotated.get("flow_3d")
        if stats["return10_median"] < market_r10 and stats["up_fraction"] < 0.5:
            strength = "weak"
        elif (stats["return10_median"] > market_r10 and stats["up_fraction"] >= 0.5
              and ((acceleration is not None and acceleration >= 0) or (flow_3d is not None and flow_3d > 0))):
            strength = "strong"
        else:
            strength = "neutral"
        overview[key] = {
            "sector_key": key, "label": labels.get(key, key), "members": stats["members"],
            "up_fraction": stats["up_fraction"], "return10_median": stats["return10_median"],
            "change_median": stats["change_median"], "limit_up": stats["limit_up"],
            "latest_breadth": rotated.get("latest_breadth"), "recent_breadth": rotated.get("recent_breadth"),
            "breadth_acceleration": acceleration, "median_change_3d": rotated.get("median_change_3d"),
            "flow_3d": flow_3d, "stable_leaders": rotated.get("stable_leaders"),
            "relative_return10": stats["return10_median"] - market_r10, "relative_strength": strength,
        }
    return dict(sorted(overview.items()))


def screen(rows: list[dict], sessions: list[str], as_of_date: str, *, events: dict[str, list[dict]] | None = None,
           price_histories: dict[str, list[dict]] | None = None,
           history_health: dict | None = None,
           information_cutoff: str | None = None,
           intraday: bool = False,
           settings: Settings = Settings()) -> dict:
    cutoff=resolve_cutoff(as_of_date,information_cutoff)
    validate_factors(settings.ranking_factors, {key for key, _, _ in LANES})
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if mainboard(row) and str(row["trade_date"]).replace("-", "") <= as_of_date.replace("-", ""):
            grouped[row["symbol"]].append(row)
    valid: dict[str, dict] = {}
    latest: dict[str, dict] = {}
    for symbol, bars in grouped.items():
        f = features(bars, sessions)
        if f:
            f['accumulation'] = evaluate_accumulation(sorted(bars, key=lambda r: str(r['trade_date'])))
            valid[symbol] = f
            latest[symbol] = max(bars, key=lambda r: str(r["trade_date"]).replace("-", ""))
    complete = bool(sessions and sessions[-1] == as_of_date and len(valid) >= settings.minimum_universe
                    and len(valid) / max(1, len(grouped)) >= settings.minimum_history_coverage)
    market_r10 = median([f["return_10d"] for f in valid.values()]) if valid else 0
    by_sector: dict[str, list[dict]] = defaultdict(list)
    sector_labels: dict[str, str] = {}
    for symbol, f in valid.items():
        key = str(latest[symbol].get("plate_id") or "unknown")
        by_sector[key].append(f)
        if key not in sector_labels and latest[symbol].get("sector_label"):
            sector_labels[key] = str(latest[symbol]["sector_label"])
    sectors = {key: {"members": len(fs), "up_fraction": sum(f["change_pct"] > 0 for f in fs)/len(fs),
                     "return10_median": median(f["return_10d"] for f in fs),
                     "change_median": median(f["change_pct"] for f in fs),
                     "limit_up": sum(f["change_pct"] >= 9.7 for f in fs)} for key, fs in by_sector.items()}
    regime = classify_regime(valid, sectors)
    rotation_by_sector = sector_rotation_metrics(valid, latest, market_r10)
    sector_overview = whole_sector_overview(sectors, rotation_by_sector, sector_labels, market_r10)
    found: dict[str, list[dict]] = {key: [] for key, _, _ in LANES}
    lane_data_gaps: dict[str, Counter] = {key: Counter() for key, _, _ in ADVANCED_LANES}
    exclusions = Counter()
    for symbol, f in valid.items():
        row = latest[symbol]
        if not complete:
            continue
        turnover = f["turnover"]
        if f["amount"] < settings.minimum_amount or turnover is None or turnover < settings.minimum_turnover:
            exclusions["low_activity"] += 1; continue
        if turnover > settings.maximum_turnover:
            exclusions["extreme_turnover"] += 1; continue
        sector_key = str(row.get("plate_id") or "unknown")
        sector = sectors[sector_key]
        breadth = sector["up_fraction"]
        board_confirmed = sector_key != "unknown" and sector["members"] >= 5 and breadth >= 0.5
        relative = f["return_10d"] - market_r10
        amount_ratio = f["amount_multiple"]
        # A partial-day amount is a lower bound, never evidence of shrinkage.
        # The vendor volume ratio can widen discovery, not authorize execution.
        vendor_ratio = row.get('volume_ratio') if intraday else None
        expansion_seen = amount_ratio >= settings.expansion_multiple or (
            isinstance(vendor_ratio, (int, float)) and vendor_ratio >= settings.expansion_multiple)
        event_activity_seen = amount_ratio >= settings.pullback_multiple or (
            isinstance(vendor_ratio, (int, float)) and vendor_ratio >= settings.pullback_multiple)
        intraday_volume_note = (f"；盘中累计额/完整日均额为下界，供应商量比{vendor_ratio if vendor_ratio is not None else '缺失'}，不是同刻成交额比" if intraday else '')
        accumulation = f['accumulation']
        matches: dict[str, tuple[bool, float, str]] = {
            "accumulation": (accumulation['matched_intersection'], accumulation['core_score'],
                             f"3/5/10日资金得分{accumulation['flow']['score']:.0f}，5/8/10日自适应横盘得分{accumulation['sideways']['score']:.0f}；5日净额{f['net5']/1e8:+.2f}亿元，成交额为前5日均值{amount_ratio:.2f}倍"),
            "expansion": (f["close"] > f["prior_high"] and expansion_seen
                          and 2 <= f["change_pct"] and board_confirmed,
                          relative + min(amount_ratio, 3) * 4 + breadth * 5,
                          f"突破前5日收盘高点，累计成交额/历史日均{amount_ratio:.2f}倍，行业上涨占比{breadth:.0%}"+intraday_volume_note),
            "pullback": (f["prior_gain"] >= 8 and relative >= 4 and -8 <= f["drawdown"] <= -1
                         and -5 <= f["change_pct"] <= 2 and amount_ratio <= settings.pullback_multiple
                         and f["close"] >= f["ma10"] * 0.98,
                         relative - abs(f["drawdown"]) + ((1 - amount_ratio) * 5 if not intraday else 0),
                         f"此前上行{f['prior_gain']:.1f}%，现回撤{abs(f['drawdown']):.1f}%，成交额比{amount_ratio:.2f}倍"+intraday_volume_note),
            "trend": (relative >= 6 and f["return_10d"] >= 5 and f["ma5"] > f["ma10"]
                      and f["close"] >= f["ma5"] and board_confirmed
                      and sector["return10_median"] > market_r10,
                      relative + breadth * 5 - max(0, f["close"] / f["ma5"] - 1) * 100,
                      f"10日涨幅{f['return_10d']:.1f}%，较市场中位数强{relative:.1f}个百分点，行业同步强于市场"),
            "relay": (f["change_pct"] >= 9.7 and board_confirmed and sector["limit_up"] >= 2,
                      breadth * 10 + min(f["net5_amount_pct"], 10) + min(amount_ratio, 3),
                      f"当日接近10%涨停，行业有{sector['limit_up']}只同类涨停；仅列次日承接观察"),
        }
        verified_events=[]
        for event in (events or {}).get(symbol,[]):
            if not (event.get('verified') is True and event.get('url') and event.get('benefit') and event.get('event_type')):
                continue
            unavailable=event_unavailable_reason(event,cutoff)
            if unavailable:
                exclusions['event_unavailable:'+unavailable]+=1
                continue
            if (date.fromisoformat(as_of_date)-date.fromisoformat(event['published_date'])).days<=7:
                verified_events.append(event)
        qualified_events = [e for e in verified_events if e.get("surprise") in {"positive", "material_positive"}
                            and e.get("priced_in") in {False, "partial"}]
        matches["event"] = (bool(qualified_events and relative > 0 and 0 < f["change_pct"]
                                  and event_activity_seen),
                            relative + min(amount_ratio, 3) * 3,
                            qualified_events[0]["benefit"] if qualified_events else
                            "事件缺少惊喜度/是否已计价字段，或尚未得到量价确认")
        sector_rotation = rotation_by_sector.get(sector_key)
        symbol_history = (price_histories or {}).get(symbol, [])
        advanced_ohlc = ohlc_features(symbol_history, as_of_date, settings.price_volume)
        # Even a timestamped history cannot be mixed with a differently adjusted
        # snapshot. Basic candidates remain visible with explicit unknown quality.
        if advanced_ohlc and abs(advanced_ohlc['close']/f['close']-1)*100 > settings.price_volume.close_match_tolerance_pct:
            advanced_ohlc = None
        advanced_details = {}
        negative_event = any(e.get("impact_direction") == "negative" for e in verified_events)
        for advanced_key, _, _ in ADVANCED_LANES:
            matched, score, reason, details = evaluate_advanced(
                advanced_key, f, ohlc=advanced_ohlc, sector_rotation=sector_rotation,
                events=verified_events, verified_negative=negative_event,
            )
            if details.get("data_gap"):
                lane_data_gaps[advanced_key][details["data_gap"]] += 1
            advanced_details[advanced_key] = details
            # Retain pre-quality discoveries as cautions for honest follow-up;
            # a weak bounce or pressure approach is not silently a passed setup.
            matches[advanced_key] = (matched or details.get('raw_match', False), score, reason)
        for key, (matched, score, reason) in matches.items():
            if not matched:
                continue
            route = route_regime(key, regime)
            if route["state"] == "disabled":
                exclusions[f"execution_disabled_observation_retained:{key}"] += 1
            # These are references derived from closes, never intraday lows or
            # executable limit prices. Every row states what still must occur.
            candidate_metrics = {**f, 'advanced': {k:v for k,v in advanced_details.get(key, {}).items() if k != 'bars'}}
            price_volume = assess_price_volume(key, f, symbol_history, as_of_date,
                advanced=advanced_details.get(key), settings=settings.price_volume)
            confirmation, invalidation = watch_conditions(key, candidate_metrics)
            crowded = f["return_10d"] >= settings.crowded_return10 or amount_ratio >= settings.crowded_amount_multiple
            weakening = (key == "accumulation" and f["change_pct"] <= -2 and amount_ratio >= 1.2)
            state = "crowded" if crowded else "wait_recovery" if weakening else "watch"
            caution = ("短期涨幅或成交额骤增较大，放在高拥挤观察，不作为优先低吸名单" if crowded else
                       "合计流入虽为正，今天却放量下跌，先等抛压减轻" if weakening else
                       "仍须看下一交易日承接；当日行业分化时不机械照抄条件")
            if state == 'watch' and price_volume['status'] == 'quality_warning':
                state = 'quality_warning'
                caution = '；'.join(price_volume['warnings'])
            elif price_volume['status'] == 'quality_unverified':
                caution += '；仅保留初筛观察，日线质量或封板过程未验证，不是量价确认通过'
            execution = execution_context(f, route)
            if route["state"] in {"restricted", "disabled"} and state == "watch":
                state = "regime_restricted"
                caution = f"当前市场状态为{regime['label']}，保留结构观察；风险预算受限，不代表可立即入场"
            if 'near_limit_up' in execution['flags']:
                caution += '；' + execution['note']
                if state == 'watch':
                    state = 'wait_next_session'
            found[key].append({"symbol": symbol, "name": row.get("name"), "lane": key,
                               "rank_score": round(score * route["priority_weight"], 4), "raw_score": round(score, 4),
                               "state": state, "reason": reason, "caution": caution,
                               "confirmation": confirmation,
                               "invalidation": invalidation,
                               "expiry": "仅供下一交易日观察，下一次收盘重算；不是持续有效买单",
                               "buy_authorized": False, "sector_key": sector_key,
                               "execution": execution,
                               "price_volume": price_volume,
                               "sector_label": row.get("sector_label") or sector_key,
                               "metrics": {k: round(v, 4) if isinstance(v, float) else v for k, v in candidate_metrics.items()},
                               "sector": sector, "events": verified_events if key in {"event", "reclaim"} else [],
                               "regime_route": route,
                               "risk_envelope": risk_envelope(key, f, regime),
                               "risk": "这是候选筛选；量价是否核验以独立证据状态为准，不是完整买入研究或交易授权"})
    factor_contexts = build_contexts(settings.ranking_factors, latest, as_of_date)
    lanes = []
    for key, label, purpose in LANES:
        rerank(found[key], weight=settings.liquidity_rank_weight, target=settings.liquidity_target_amount)
        factor_policy = apply_factors(found[key], key, settings.ranking_factors, factor_contexts)
        observed = observation_list(found[key], limit=settings.top_per_lane,
                                    per_industry=settings.maximum_per_industry)
        ranked = sorted(found[key], key=lambda r: (-r["rank_score"], r["symbol"]))
        caution_list = [r for r in ranked if r["state"] != "watch"][:3]
        selected, counts = [], Counter()
        for item in ranked:
            if item["state"] != "watch":
                continue
            if counts[item["sector_key"]] >= settings.maximum_per_industry:
                continue
            selected.append(item); counts[item["sector_key"]] += 1
            if len(selected) == settings.top_per_lane:
                break
        lane_gaps = dict(lane_data_gaps.get(key, {}))
        lane_status = "completed" if complete and (key not in {"contraction", "reclaim"} or (history_health or {}).get("ready", 0) > 0) else "data_gap"
        from ..effectiveness.capture import features as effectiveness_features
        lanes.append({"key": key, "label": label, "purpose": purpose,
                      "tracking_candidates": [{**{k:v for k,v in r.items() if k not in ('events','sector','metrics')},
                          "effectiveness_features": effectiveness_features(r,key),
                          "metrics":{k:v for k,v in r['metrics'].items() if k not in ('flow_series','return_series','accumulation')}} for r in ranked],
                      **({'factor_policy': factor_policy} if factor_policy else {}),
                      "status": lane_status, "total_matches": len(ranked),
                      "selected": selected, "caution_list": caution_list,
                      "observation_list": observed,
                      "observation_policy": "按结构与交易基础排序，不因市场风险倍率或接近涨停删除；风险状态与交易条件另列。可选因子仅影响原条件名单，不改此独立观察顺序。",
                      "data_gaps": lane_gaps,
                      "regime_route": route_regime(key, regime),
                      "empty_reason": ("候选严格OHLC补充不足，不能用收盘快照伪造高低点、ATR或假跌破" if lane_status == "data_gap" else
                                       "没有满足优先观察状态的标的；查看过热或转弱名单，不降低门槛凑数" if complete else
                                       "历史截面未齐，不能用缺失交易日假装连续走势")})
    observations = []
    single_flow, single_sideways = [], []
    for symbol, f in valid.items():
        if not complete:
            continue
        a = f['accumulation']
        identity = {'symbol': symbol, 'name': latest[symbol].get('name'), 'amount': f['amount']}
        if a['matched_flow']:
            single_flow.append(identity)
        if a['matched_sideways']:
            single_sideways.append(identity)
        if a['matched_intersection'] or a['near_match']:
            observations.append({**identity, **a, 'buy_authorized': False,
                'activity_eligible': f['amount'] >= settings.minimum_amount and f['turnover'] is not None and settings.minimum_turnover <= f['turnover'] <= settings.maximum_turnover})
    observations.sort(key=lambda r: (not r['activity_eligible'], -r['core_score'], r['symbol']))
    return {"version": VERSION, "as_of_date": as_of_date, "status": "completed" if complete else "data_gap",
            "information_cutoff":cutoff.isoformat(),
            "event_time_scope":"仅使用截至该时点已经可得的消息生成下一交易日观察；不是当日入场回测。无时区或事后可得消息不参与事件确认。",
            "accumulation_observations": observations,
            "accumulation_single_conditions": {'flow': single_flow, 'sideways': single_sideways},
            "research_only": True, "settings": asdict(settings), "sessions": sessions,
            "coverage": {"universe": len(grouped), "complete_history": len(valid), "excluded": dict(exclusions)},
            "market": {"median_return10": market_r10, "up_fraction": sum(f["change_pct"] > 0 for f in valid.values())/len(valid) if valid else None,
                       "regime": regime},
            # Whole-sector reference for the recommendation layer; a label, not a
            # score, and deliberately outside the scan hash evidence set.
            "sector_overview": sector_overview,
            "history_enrichment": history_health or {"requested": 0, "ready": 0, "failed": 0},
            "risk_policy": {"separate_from_alpha": True, "t_plus_one": True, "cost_aware": True,
                            "portfolio_and_sector_caps": True, "time_and_trailing_stops": True},
            "lanes": lanes, "notice": "九条独立观察筛选，不合并为总分；市场状态只路由策略，风险层不参与alpha评分；资金为供应商订单大小分类，不能等同机构身份。"}
