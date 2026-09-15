"""Strategy-driven single-stock workbench assembly.

This endpoint supplies visual research evidence and conditional scenarios.  It
never turns an indicator into an executable order.  Exact trading instructions
are shown only when an unexpired persisted personal trade plan exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from .stock_workbench_contracts import STRATEGY_CONTRACTS, strategy_catalog
from .stock_workbench_indicators import enrich_daily_bars, number, weekly_bars
from .user_tracking_research import build_snapshot as build_user_tracking_snapshot
from .short_term_lanes.regime import route as route_regime
from .short_term_lanes.risk import risk_envelope


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class StockWorkbenchDependencies:
    china_today: Callable[[], date]
    run_provider: Callable[..., Awaitable[Any]]
    evidence: Callable[..., Awaitable[dict[str, Any]]]
    source_factory: Callable[[], Any]


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _local_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(SHANGHAI).date() if parsed.tzinfo else parsed.date()
        except ValueError:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
    return None


def parse_longhu_history(envelope: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Normalize GetKLineDay_W14 without guessing undocumented fields."""
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    pages = envelope.get("pages") if isinstance(envelope, dict) else None
    for page in pages if isinstance(pages, list) else []:
        payload = page.get("payload") if isinstance(page, dict) else None
        if not isinstance(payload, dict):
            errors.append("page_missing_payload")
            continue
        dates, values = payload.get("x"), payload.get("y")
        if not isinstance(dates, list) or not isinstance(values, list) or len(dates) != len(values):
            errors.append("history_axes_mismatch")
            continue
        volumes = payload.get("vol") if isinstance(payload.get("vol"), list) else []
        amounts = payload.get("bal") if isinstance(payload.get("bal"), list) else []
        turnovers = payload.get("turnover") if isinstance(payload.get("turnover"), list) else []
        for index, stamp in enumerate(dates):
            vector = values[index]
            if not isinstance(vector, list) or len(vector) < 4:
                errors.append(f"invalid_ohlc:{stamp}")
                continue
            text = str(stamp)
            normalized_date = f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text
            rows.append({
                "date": normalized_date,
                # Provider contract: y = [open, close, high, low].
                "open": number(vector[0]), "close": number(vector[1]),
                "high": number(vector[2]), "low": number(vector[3]),
                "volume": number(volumes[index]) if index < len(volumes) else None,
                "amount": number(amounts[index]) if index < len(amounts) else None,
                "turnover_rate": number(turnovers[index]) if index < len(turnovers) else None,
                "source": "longhuvip:GetKLineDay_W14",
            })
    rows.sort(key=lambda row: row["date"])
    health = {
        "status": "ready" if rows and not errors else "partial" if rows else "unavailable",
        "rows": len(rows), "errors": errors[:20], "physical_batch_limit": envelope.get("physical_batch_limit"),
        "calls": envelope.get("calls"), "price_basis": "provider_front_adjusted_request:Is_FS=1",
    }
    return rows, health


def _fetch_history(source_factory: Callable[[], Any], symbol: str, lookback_days: int) -> dict[str, Any]:
    source = source_factory()
    return source.raw_call({
        "target": "longhu_history",
        "path": "/w1/api/index.php",
        "params": {
            "a": "GetKLineDay_W14", "c": "StockLineData", "apiv": "w40",
            "StockID": symbol.split(".", 1)[0], "Type": "d", "Is_FS": "1",
            "st": min(300, lookback_days), "Index": 0,
        },
    })


def _category(event: dict[str, Any]) -> str:
    text = f"{event.get('event_type', '')} {event.get('title', '')}"
    if any(word in text for word in ("监管", "问询", "处罚", "立案")):
        return "regulatory"
    if any(word in text for word in ("减持", "解禁", "质押", "回购", "增持", "融资")):
        return "capital"
    if any(word in text for word in ("中标", "订单", "合同", "合作", "获批", "产能")):
        return "catalyst"
    if any(word in text for word in ("业绩", "年报", "季报", "快报", "利润", "营收")):
        return "company"
    if any(word in text for word in ("涨停", "异动", "龙虎榜", "热度")):
        return "market"
    return "risk" if "风险" in text else "company"


def _messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in reversed(events):
        category = _category(event)
        source = str(event.get("source") or "unknown")
        url = str(event.get("url") or "")
        official = any(token in f"{source} {url}".lower() for token in ("cninfo", "sse.com", "szse.cn", "exchange"))
        body = " ".join(str(event.get("body") or "").split())
        relevant = [contract.key for contract in STRATEGY_CONTRACTS if category in contract.message_categories]
        result.append({
            "id": str(event.get("event_id") or ""), "category": category,
            "event_type": str(event.get("event_type") or "未分类"), "title": str(event.get("title") or ""),
            "detail": body[:220], "source": source, "url": event.get("url"),
            "occurred_at": _iso(event.get("occurred_at")), "available_at": _iso(event.get("available_at")),
            "chart_date": _iso(event.get("occurred_at"))[:10] if event.get("occurred_at") else None,
            "verification": "一手披露" if official else "市场线索，需回查一手来源",
            "impact": "未由确定性数据判定方向，需放入所选策略的价格、量能与板块条件中复核",
            "relevant_strategies": relevant,
        })
    return result


def _market_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"status": "unavailable", "rows": []}
    compounded = 1.0
    normalized = []
    for row in rows[-20:]:
        change = number(row.get("median_change_pct"))
        if change is not None:
            compounded *= 1 + change / 100
        normalized.append({**row, "trading_date": _iso(row.get("trading_date"))})
    return {
        "status": "ready", "rows": normalized,
        "median_compounded_20d_pct": (compounded - 1) * 100,
        "latest": normalized[-1],
    }


def _flow_context(rows: list[dict[str, Any]], as_of_date: str) -> dict[str, Any]:
    normalized = [{**row, "trading_date": _iso(row.get("trading_date")), "available_at": _iso(row.get("available_at"))} for row in rows]
    values = [number(row.get("net_amount")) for row in normalized]
    valid = [value for value in values if value is not None]
    latest_date = normalized[-1]["trading_date"] if normalized else None
    windows: dict[str, Any] = {}
    for window in (1, 3, 5, 10):
        sample = [value for value in values[-window:] if value is not None]
        windows[str(window)] = {
            "net_amount": sum(sample) if len(sample) == min(window, len(values)) and sample else None,
            "positive_days": sum(value > 0 for value in sample), "observations": len(sample),
        }
    status = "ready" if latest_date == as_of_date and len(valid) >= 5 else "partial" if valid else "unavailable"
    return {
        "status": status, "series": normalized, "windows": windows,
        "source": "longhuvip_main_net", "provider": "longhuvip_composite",
        "semantic_boundary": "供应商按成交单规模归类的资金净额，不等同真实机构身份、暗盘或Level-2撤单",
    }


def _health(
    *, history: dict[str, Any], technical: dict[str, Any], flow: dict[str, Any],
    sectors: list[dict[str, Any]], messages: list[dict[str, Any]], plan: dict[str, Any] | None,
    market: dict[str, Any], latest: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    amount_ready = bool(latest and number(latest.get("amount")) is not None)
    turnover_ready = bool(latest and number(latest.get("turnover_rate")) is not None)
    mapping = {
        "price": (history["status"], f"{history['rows']}根严格OHLC日线"),
        "volume": ("ready" if amount_ready else "unavailable", "成交额与成交量"),
        "technical": (technical["status"], "MA/ATR/MACD/KDJ/RSI/布林"),
        "vendor_flow": (flow["status"], "Longhu成交单规模资金"),
        "turnover": ("ready" if turnover_ready else "unavailable", "换手率"),
        "sector": ("ready" if any(row.get("trading_date") for row in sectors) else "partial" if sectors else "unavailable", "点时板块归属与资金状态"),
        "messages": (
            "ready" if messages else "unavailable",
            f"{len(messages)}条可追溯事件" if messages else "未取得可追溯事件；不能解释为公司没有消息",
        ),
        "trade_plan": ("ready" if plan else "checked_no_signal", "当前有效的持仓/新买计划" if plan else "没有当前有效交易计划"),
        "scenario": ("ready" if technical["status"] in {"ready", "partial"} and history["rows"] >= 10 else "unavailable", "条件化次日与下周情景"),
        "relative_strength": (market["status"], "相对全市场收益中位数"),
        "limit_structure": ("ready" if latest else "unavailable", "涨跌幅与价格结构；涨停梯队另由收盘策略报告提供"),
    }
    return {key: {"status": status, "detail": detail} for key, (status, detail) in mapping.items()}


def _scenario_views(
    technical: dict[str, Any], flow: dict[str, Any], market: dict[str, Any],
    health: dict[str, dict[str, Any]], plan: dict[str, Any] | None,
    messages: list[dict[str, Any]], sectors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summary = technical.get("summary") or {}
    close = number(summary.get("close"))
    support = number(summary.get("support_5"))
    resistance = number(summary.get("resistance_10"))
    atr = number(summary.get("atr14"))
    volume_multiple = number(summary.get("amount_multiple_5"))
    market_return = number(market.get("median_compounded_20d_pct"))
    stock_return = number(summary.get("return_20d_pct"))
    relative = stock_return - market_return if stock_return is not None and market_return is not None else None
    flow5 = number((flow.get("windows") or {}).get("5", {}).get("net_amount"))
    latest = (technical.get("bars") or [{}])[-1]
    ma5 = number(summary.get("ma5"))
    ma20 = number(summary.get("ma20"))
    rsi = number(summary.get("rsi14"))
    turnover = number(latest.get("turnover_rate"))
    sector_labels = [str(row.get("label")) for row in sectors if row.get("label")][:3]
    sector_text = "、".join(sector_labels) if sector_labels else "所属板块"
    result = []
    for contract in STRATEGY_CONTRACTS:
        blockers = [
            panel for panel in contract.required_panels
            if health.get(panel, {}).get("status") != "ready"
            and not (health.get(panel, {}).get("status") == "checked_no_signal" and panel != "messages")
        ]
        if close is None or support is None or resistance is None:
            result.append({
                "key": contract.key, "status": "data_gap", "blockers": sorted(set(blockers + ["price", "scenario"])),
                "current_reading": ["严格价格窗口不足，不能生成价格情景"], "next_session": [], "next_week": [],
            })
            continue
        cushion = atr * 0.35 if atr is not None else close * 0.012
        breakout = max(resistance, close + cushion)
        failure = min(support, close - cushion)
        reading = [
            f"最新收盘 {close:.2f}，5日结构支撑 {support:.2f}，10日压力 {resistance:.2f}",
            f"成交额相对前5日均值 {volume_multiple:.2f}倍" if volume_multiple is not None else "成交额基线不足",
        ]
        if contract.key == "user_tracking":
            reading.append(
                f"近5日成交单规模资金合计 {flow5 / 10_000:.0f}万元；{sector_text}与个股必须一起复核"
                if flow5 is not None else f"近5日资金数据不足；仍需结合{sector_text}与公告风险复核"
            )
        elif contract.key == "accumulation":
            reading.append(f"5日成交单规模资金合计 {flow5 / 10_000:.0f}万元" if flow5 is not None else "5日同口径资金不足")
        elif contract.key in {"trend", "pullback", "rotation"}:
            reading.append(f"20日相对市场收益中位数 {relative:+.2f}个百分点" if relative is not None else "相对市场序列不足")
        elif contract.key == "event":
            reading.append(f"当前策略命中 {len(messages)} 条可追溯事件；事件只作为时间锚，必须由量价与板块响应确认")
        elif contract.key == "relay":
            reading.append(f"最新换手率 {turnover:.2f}%" if turnover is not None else "换手率证据不足")
        elif contract.key == "contraction":
            reading.append(f"布林带宽、ATR和成交额收缩必须同时成立，最新成交额比{volume_multiple:.2f}倍" if volume_multiple is not None else "收缩结构的成交额基线不足")
        elif contract.key == "reclaim":
            reading.append("急跌后的收复只在没有已核验利空、板块止跌且修复成交额改善时有效")
        else:
            reading.append(f"RSI14 {rsi:.1f}，当前读数只描述结构，不构成次日上涨概率" if rsi is not None else "当前读数只描述结构，不构成次日上涨概率")

        execution_note = (
            f"存在有效{plan.get('plan_kind')}计划，实际动作必须逐字服从该计划"
            if plan else "当前没有有效交易计划；触发后只升级研究，不自动获得买入权限"
        )
        strategy_conditions = {
            "user_tracking": (
                f"收盘重新站稳MA20 {ma20:.2f}和压力 {breakout:.2f}，近5日资金改善且{sector_text}不转弱" if ma20 is not None else f"收盘站稳压力 {breakout:.2f}，近5日资金改善且{sector_text}不转弱",
                f"守住 {support:.2f} 后缩量整理，资金流出收窄且没有新增已核验利空",
                f"放量跌破 {failure:.2f} 后不能快速收复，或出现新的已核验公司风险",
                ["价格结构", "成交额与换手", "1/5/10日资金", sector_text, "一手公告与风险"],
            ),
            "accumulation": (
                f"站上 {breakout:.2f}，5日同口径资金保持净流入，成交额温和放大但不超过近期极值",
                f"守住 {support:.2f} 横盘，5日资金没有转成连续流出",
                f"跌破 {failure:.2f} 且5日资金转负，潜伏结构失效",
                ["5日资金方向", "区间宽度", "压力位抛压", sector_text],
            ),
            "expansion": (
                f"有效站上 {breakout:.2f}，成交额至少为5日均值 {contract.volume_confirmation:.2f} 倍，{sector_text}同步增强",
                f"在 {support:.2f}–{resistance:.2f} 蓄势，量能尚未达到启动门槛",
                f"放量跌破 {failure:.2f}，或突破后快速缩量跌回平台",
                ["突破有效性", "量能持续性", "换手是否过热", sector_text],
            ),
            "pullback": (
                f"回踩后重新站上 {ma5:.2f} 并收复 {breakout:.2f}，下跌段缩量、修复段放量" if ma5 is not None else f"回踩后收复 {breakout:.2f}，下跌段缩量、修复段放量",
                f"在 {support:.2f} 上方缩量消化，未出现板块共振走弱",
                f"放量跌破 {failure:.2f} 且无法快速收复，强势回踩假设失效",
                ["回踩缩量", "承接成交额", "MA20趋势", "相对市场强弱"],
            ),
            "trend": (
                f"站稳 {breakout:.2f}，维持在MA20 {ma20:.2f}上方，且相对市场继续为正" if ma20 is not None else f"站稳 {breakout:.2f}，相对市场与板块继续增强",
                f"在 {support:.2f}–{resistance:.2f} 震荡，但相对市场和{sector_text}不转弱",
                f"跌破 {failure:.2f}，同时相对市场和板块趋势转弱",
                ["MA20斜率", "相对市场收益", sector_text, "成交额趋势"],
            ),
            "event": (
                f"已核验事件仍在有效期，价格站上 {breakout:.2f}，成交额至少为5日均值 {contract.volume_confirmation:.2f} 倍",
                f"事件已披露但价格停留 {support:.2f}–{resistance:.2f}，市场尚未确认方向",
                f"事件证伪/兑现不及预期，或价格放量跌破 {failure:.2f}",
                ["一手来源", "事件有效期", "量价响应", sector_text],
            ),
            "relay": (
                f"站上 {breakout:.2f}，换手和成交额形成承接而非单边加速，板块高位晋级率不恶化",
                f"在 {support:.2f}–{resistance:.2f} 高换手消化，封板/开板结构未明显退潮",
                f"跌破 {failure:.2f}，同时出现放量分歧和板块退潮",
                ["封板/开板", "换手承接", "板块晋级率", "监管与减持"],
            ),
            "contraction": (
                f"真实波幅与成交额完成收缩后，收盘站稳 {breakout:.2f}，成交额至少为5日均值 {contract.volume_confirmation:.2f} 倍且{sector_text}广度改善",
                f"在 {support:.2f}–{resistance:.2f} 继续收缩，等待第一次有效放量而不是区间中部预判",
                f"突破失败跌回 {resistance:.2f} 下方，或放量跌破 {failure:.2f}",
                ["ATR/布林带宽收缩", "成交额先缩后放", "首次突破", sector_text],
            ),
            "rotation": (
                f"{sector_text}连续3日广度和资金改善，个股站稳 {breakout:.2f} 但未脱离板块单独加速",
                f"个股在 {support:.2f}–{resistance:.2f} 整理，同时观察行业中位个股是否继续改善",
                f"行业广度跌回半数以下且资金转负，或个股放量跌破 {failure:.2f}",
                ["行业3日广度", "行业资金排名变化", "中位个股", "先行股稳定性"],
            ),
            "reclaim": (
                f"急跌/假跌破后重新站稳 {breakout:.2f}，修复段成交额强于下跌段且{sector_text}止跌",
                f"守住 {support:.2f} 消化抛压，未出现新的已核验负面事件",
                f"再次跌破 {failure:.2f} 且不能快速收复，或发现已核验公司利空",
                ["急跌原因核验", "收复位置", "下跌与修复成交额", sector_text],
            ),
        }
        up_condition, range_condition, down_condition, checkpoints = strategy_conditions[contract.key]
        next_session = [
            {"state": "向上确认", "condition": up_condition, "action": f"确认本策略的价格、量能和外部条件同时成立；{execution_note}", "invalidation": f"重新跌回 {resistance:.2f} 下方且确认信号消失"},
            {"state": "区间消化", "condition": range_condition, "action": "等待结构选择方向，不在区间中部追价；只复核本策略声明的证据", "invalidation": f"有效跌破 {support:.2f} 或突破 {resistance:.2f}"},
            {"state": "向下失效", "condition": down_condition, "action": f"取消本策略观察；{execution_note}", "invalidation": f"快速收复 {support:.2f} 且本策略确认条件恢复"},
        ]
        next_week = [
            {"state": "趋势延续", "condition": f"一周内至少两次收盘站稳 {resistance:.2f} 上方，且本策略确认条件持续", "action": "仅在已完成公司研究和有效交易计划时按回踩确认执行；否则保留研究优先级", "checkpoints": checkpoints},
            {"state": "箱体震荡", "condition": f"大部分时间停留在 {support:.2f}–{resistance:.2f}", "action": "维持观察，靠近边界才重新评估赔率；区间中部不产生机械动作", "checkpoints": ["支撑附近缩量", "压力附近放量", "相对市场强弱"]},
            {"state": "结构破坏", "condition": f"周内有效跌破 {failure:.2f}，或相对市场和板块同时明显走弱", "action": "该策略当周失效；持仓处理只读取有效持仓计划，不把图形参考线冒充止损", "checkpoints": ["是否为除权/数据异常", "板块是否共振下跌", "资金是否单日集中流出"]},
        ]
        regime_label = "broad_risk_on" if (market_return or 0) >= 2 else "risk_off" if (market_return or 0) <= -2 else "mixed_rotation"
        regime = {"label": regime_label, "research_budget": 1.0 if regime_label == "broad_risk_on" else 0.35 if regime_label == "risk_off" else 0.7,
                  "strategy_priority": {}}
        result.append({
            "key": contract.key, "status": "ready" if not blockers else "degraded",
            "blockers": blockers, "current_reading": reading, "next_session": next_session, "next_week": next_week,
            "levels": {"support": support, "resistance": resistance, "breakout_confirmation": breakout, "failure": failure},
            "regime_route": route_regime(contract.key, regime),
            "risk_envelope": risk_envelope(contract.key, {"close": close, "recent_low": support, "volatility": 0.0}, regime),
            "probability_status": "not_calibrated", "probability_note": "未取得该策略足够的独立前向样本，不输出伪概率",
        })
    return result


async def build(symbol: str, request: Any, deps: StockWorkbenchDependencies) -> dict[str, Any]:
    requested_as_of_date = request.as_of_date or deps.china_today()
    lookback = min(300, max(30, int(request.lookback_days)))
    # The price axis is bounded by as_of_date, while evidence is bounded by an
    # explicit knowledge cutoff.  By default the workbench is a current
    # decision surface and may show announcements learned after the latest
    # close (for example over a weekend).  Replays can supply a historical
    # cutoff and remain free of look-ahead.
    knowledge_cutoff = getattr(request, "knowledge_cutoff", None) or datetime.now(timezone.utc)
    evidence_task = asyncio.create_task(deps.evidence(
        symbol, requested_as_of_date, lookback_days=lookback, knowledge_cutoff=knowledge_cutoff,
    ))
    history_error: str | None = None
    try:
        envelope = await deps.run_provider(_fetch_history, deps.source_factory, symbol, lookback, timeout_seconds=30)
        history_rows, history_health = parse_longhu_history(envelope)
    except Exception as error:  # one provider failure must remain diagnosable
        history_rows, history_health = [], {"status": "unavailable", "rows": 0, "errors": [f"{type(error).__name__}: {error}"], "price_basis": "unavailable"}
        history_error = str(error)
    history_rows = [row for row in history_rows if row["date"] <= str(requested_as_of_date)]
    effective_as_of_date = date.fromisoformat(history_rows[-1]["date"]) if history_rows else requested_as_of_date
    history_health["rows"] = len(history_rows)
    history_health["latest_date"] = history_rows[-1]["date"] if history_rows else None
    history_health["status"] = (
        "ready" if len(history_rows) >= 21 else "partial" if history_rows else "unavailable"
    )
    evidence = await evidence_task

    fundamentals = {str(row.get("trading_date")): row for row in evidence["fundamentals"]}
    merged_rows = []
    for row in history_rows:
        basic = fundamentals.get(row["date"]) or {}
        merged_rows.append({**row, "turnover_rate": row.get("turnover_rate") if row.get("turnover_rate") is not None else basic.get("turnover_rate"), "volume_ratio": basic.get("volume_ratio"), "pe": basic.get("pe"), "pb": basic.get("pb")})
    technical = enrich_daily_bars(merged_rows)
    daily = technical.get("bars") or []
    weekly_raw = weekly_bars(daily)
    weekly = enrich_daily_bars(weekly_raw)
    market = _market_context(evidence["market"])
    flow = _flow_context(evidence["flows"], str(effective_as_of_date))
    messages = _messages(evidence["events"])
    latest = daily[-1] if daily else None
    raw_plan = evidence["active_plan"]
    plan_date = _local_date(raw_plan.get("as_of_at")) if isinstance(raw_plan, dict) else None
    plan_status = "missing"
    plan_reason = "没有当前交易计划"
    active_plan = raw_plan
    if raw_plan:
        if plan_date is None:
            active_plan = None
            plan_status = "invalid"
            plan_reason = "交易计划缺少可解析的数据日期"
        elif plan_date < effective_as_of_date:
            active_plan = None
            plan_status = "stale"
            plan_reason = f"交易计划基于{plan_date}，落后于最新行情{effective_as_of_date}"
        else:
            plan_status = "current"
            plan_reason = f"交易计划与最新行情同为{effective_as_of_date}"
    health = _health(
        history=history_health, technical=technical, flow=flow, sectors=evidence["sectors"],
        messages=messages, plan=active_plan, market=market, latest=latest,
    )
    if plan_status in {"stale", "invalid"}:
        health["trade_plan"] = {"status": plan_status, "detail": plan_reason}
    views = _scenario_views(
        technical, flow, market, health, active_plan, messages, evidence["sectors"],
    )
    instrument = evidence["instrument"] or {"symbol": symbol, "name": symbol, "industry": None}
    # Canonical instrument metadata is not guaranteed to carry an industry.
    # The workbench already has a point-in-time observed sector membership, so
    # surface that label instead of showing a misleading empty industry field.
    observed_industry = next(
        (str(row.get("label")) for row in evidence["sectors"] if row.get("label")),
        None,
    )
    industry = instrument.get("industry") or observed_industry
    result = {
        "contract_version": "stock-workbench-v2",
        "symbol": symbol, "name": instrument.get("name") or symbol, "industry": industry,
        "as_of_date": str(effective_as_of_date), "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True, "live_effect": "none",
        "strategies": strategy_catalog(), "strategy_views": views,
        "series": {"daily": daily, "weekly": weekly.get("bars") or []},
        "technical_summary": technical.get("summary") or {}, "flow": flow,
        "messages": messages, "sectors": [{**row, "trading_date": _iso(row.get("trading_date")), "available_at": _iso(row.get("available_at"))} for row in evidence["sectors"]],
        "market_context": {**market, "regime": evidence["regime"], "sentiment": evidence["sentiment"]},
        "active_trade_plan": active_plan, "data_health": health,
        "artifact_freshness": {
            "price": {"status": history_health["status"], "as_of_date": history_health.get("latest_date")},
            "flow": {"status": flow["status"], "as_of_date": (flow.get("series") or [{}])[-1].get("trading_date")},
            # _messages returns newest first.  Using the tail here made the UI
            # claim that fresh announcements were stale.
            "messages": {"status": "current" if messages else "unverified", "latest_available_at": messages[0].get("available_at") if messages else None},
            "sector": {"status": health["sector"]["status"], "as_of_date": _iso(evidence["sectors"][0].get("trading_date")) if evidence["sectors"] else None},
            "market": {"status": market["status"], "as_of_date": (market.get("latest") or {}).get("trading_date")},
            "trade_plan": {"status": plan_status, "as_of_date": _iso(raw_plan.get("as_of_at")) if isinstance(raw_plan, dict) else None, "detail": plan_reason},
        },
        "source_health": {"history": history_health, "history_error": history_error},
        "notice": "图中价格、量能、成交单规模资金、事件与情景按策略选择性展示；没有有效交易计划时，任何价格条件都只是研究触发器。",
    }
    result["tracking_analysis"] = build_user_tracking_snapshot(result)
    return result


__all__ = ["StockWorkbenchDependencies", "build", "parse_longhu_history"]
