"""Close bounded short-term research and materialize human execution plans.

This module intentionally separates candidate discovery, bullish scoring and
the independent downside gate.  It never submits broker orders.  A failed
candidate dossier is persisted for audit but cannot become a new-buy plan;
actual holdings always receive a defensive plan so the user is never shown a
vague "research pending" placeholder instead of risk controls.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from .decision_research_contracts import (
    DecisionResearchDossier,
    GATE_LABELS,
    RESEARCH_MODEL_VERSION,
    ResearchGate,
    terminal_status,
)
from .decision_research_repository import (
    holding_evidence,
    latest_candidate_evidence,
    latest_exact_portfolio,
    persist_dossier,
)
from .personal_decision_contracts import PersonalTradePlanInput, PriceZone
from .personal_decision_repository import persist_trade_plan


SHANGHAI = ZoneInfo("Asia/Shanghai")
STRATEGY_FAMILY = "short_term_market_structure"
HOLDING_SHORT_TERM_CAP_PCT = Decimal("25")


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _price(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _daily_basic(evidence: dict[str, Any]) -> dict[str, Any]:
    value = evidence.get("daily_basic")
    return dict(value) if isinstance(value, dict) else {}


def _theme(evidence: dict[str, Any]) -> str | None:
    raw = evidence.get("flow_raw")
    if not isinstance(raw, dict):
        return None
    vendor = raw.get("vendor_row")
    if isinstance(vendor, list) and len(vendor) > 4:
        value = str(vendor[4] or "").strip()
        return value or None
    return None


def _board(evidence: dict[str, Any]) -> dict[str, Any]:
    value = evidence.get("board_context")
    return dict(value) if isinstance(value, dict) else {}


def _geometry_from_structure(evidence: dict[str, Any]) -> dict[str, float] | None:
    structure = evidence.get("structure")
    metrics = structure.get("metrics", {}) if isinstance(structure, dict) else {}
    close = _float(evidence.get("close") or _daily_basic(evidence).get("close"))
    support = _float(metrics.get("support_price"))
    resistance = _float(metrics.get("resistance_price"))
    sma20 = _float(metrics.get("sma20"))
    recent_range = _float(metrics.get("recent_range_pct"))
    if not all(value and value > 0 for value in (close, support, resistance, sma20)):
        return None
    return {
        "close": close, "support": support, "resistance": resistance, "sma20": sma20,
        "volatility_pct": max(1.0, recent_range or 4.0),
    }


def _geometry_from_bars(evidence: dict[str, Any]) -> dict[str, float] | None:
    bars = [dict(row) for row in evidence.get("bars", []) if isinstance(row, dict)]
    if len(bars) < 10:
        return None
    recent = bars[-10:]
    closes = [_float(row.get("close")) for row in bars[-20:]]
    highs = [_float(row.get("high")) for row in recent]
    lows = [_float(row.get("low")) for row in recent]
    if any(value is None or value <= 0 for value in [*closes, *highs, *lows]):
        return None
    clean_closes = [value for value in closes if value is not None]
    clean_highs = [value for value in highs if value is not None]
    clean_lows = [value for value in lows if value is not None]
    close = _float(evidence.get("close")) or clean_closes[-1]
    ranges = [(high - low) / prior * 100 for high, low, prior in zip(clean_highs, clean_lows, clean_closes[-10:])]
    return {
        "close": close,
        "support": min(clean_lows),
        "resistance": max(clean_highs),
        "sma20": sum(clean_closes) / len(clean_closes),
        "volatility_pct": max(1.0, sum(ranges) / len(ranges)),
    }


def _trade_geometry(base: dict[str, float], *, holding: bool) -> dict[str, Any]:
    close, support, resistance = base["close"], base["support"], base["resistance"]
    volatility = base["volatility_pct"] / 100
    risk_pct = max(0.035, min(0.07, volatility * 1.8))
    if holding:
        entry_lower = close
        stop = max(support * 0.99, close * (1 - risk_pct))
        stop = min(stop, close * 0.985)
        entry_upper = close
    else:
        entry_lower = max(close, resistance * 1.002)
        entry_upper = entry_lower * 1.018
        stop = max(support * 0.99, entry_lower * (1 - risk_pct))
        stop = min(stop, entry_lower * 0.975)
    actual_risk = max(0.01, entry_lower - stop)
    midpoint = (entry_lower + entry_upper) / 2
    target1 = max(resistance, midpoint + actual_risk * 2)
    target2 = midpoint + actual_risk * 3
    return {
        **base,
        "entry_lower": round(entry_lower, 4), "entry_upper": round(entry_upper, 4),
        "stop": round(stop, 4), "risk_pct": round((entry_lower - stop) / entry_lower * 100, 3),
        "target1": round(target1, 4), "target2": round(target2, 4),
        "reward_risk_1": round((target1 - midpoint) / actual_risk, 3),
        "reward_risk_2": round((target2 - midpoint) / actual_risk, 3),
    }


def independent_downside_gate(geometry: dict[str, Any] | None, risk_flags: list[str]) -> ResearchGate:
    """Run G6 from downside geometry only, without bullish scores or rationale."""
    if geometry is None:
        return ResearchGate(
            gate_key="G6", label=GATE_LABELS["G6"], verdict="unknown", independent_run=True,
            conclusion="缺少足够的价格历史，无法独立计算失效位和单笔风险。",
            evidence={"risk_flags": risk_flags},
        )
    risk_pct = float(geometry["risk_pct"])
    hard_failures = [flag for flag in risk_flags if flag in {"st_or_delisting_risk", "not_mainboard"}]
    verdict = "fail" if hard_failures or risk_pct > 7.5 or risk_pct < 1.5 else "pass"
    conclusion = (
        f"按入场下沿测算，硬失效位风险为 {risk_pct:.2f}%；"
        f"若次日直接低开并跌破 {geometry['stop']:.2f}，不以回本为理由延后退出。"
    )
    return ResearchGate(
        gate_key="G6", label=GATE_LABELS["G6"], verdict=verdict, independent_run=True,
        conclusion=conclusion,
        evidence={
            "entry_lower": geometry["entry_lower"], "stop": geometry["stop"],
            "risk_pct": risk_pct, "gap_down_stress_pct": round(risk_pct + 3.0, 3),
            "hard_failures": hard_failures, "risk_flags": risk_flags,
            "independence_boundary": "bullish score and candidate rank were not inputs",
        },
    )


def build_dossier(
    evidence: dict[str, Any], *, as_of_date: date, holding: bool,
    position: dict[str, Any] | None = None,
) -> DecisionResearchDossier:
    symbol = str(evidence["symbol"])
    name = str(evidence.get("name") or symbol)
    basic, board, theme = _daily_basic(evidence), _board(evidence), _theme(evidence)
    amount = _float(evidence.get("amount"))
    turnover = _float(basic.get("turnover_rate"))
    volume_ratio = _float(basic.get("volume_ratio"))
    pe, pb = _float(basic.get("pe")), _float(basic.get("pb"))
    main_net = _float(evidence.get("main_net_amount"))
    board_net = _float(board.get("net_amount") if "net_amount" in board else board.get("net_inflow"))
    board_percentile = _float(board.get("flow_percentile"))
    exact_mapping = bool(board.get("exact_member_mapping", True) and board.get("sector_key"))
    geometry_base = _geometry_from_bars(evidence) if holding else _geometry_from_structure(evidence)
    geometry = _trade_geometry(geometry_base, holding=holding) if geometry_base else None
    risk_flags = [str(flag) for flag in evidence.get("risk_flags", [])]
    if not (symbol.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
        risk_flags.append("not_mainboard")
    if "ST" in name.upper() or "退" in name:
        risk_flags.append("st_or_delisting_risk")
    legacy = evidence.get("legacy_terminal_research")
    legacy_recent_rejected = False
    if isinstance(legacy, dict) and legacy.get("status") == "rejected" and legacy.get("as_of"):
        try:
            legacy_recent_rejected = 0 <= (as_of_date - date.fromisoformat(str(legacy["as_of"]))).days <= 14
        except ValueError:
            legacy_recent_rejected = False
    if legacy_recent_rejected:
        risk_flags.append("recent_legacy_terminal_research_rejected")
    valuation_pass = pe is not None and 0 < pe <= 120 and pb is not None and pb <= 12
    g5_pass = bool(
        geometry and amount is not None and amount >= 120_000_000
        and turnover is not None and 1.5 <= turnover <= 25
        and (volume_ratio is None or volume_ratio <= 4.5)
    )

    gates = [
        ResearchGate(
            gate_key="G0", label=GATE_LABELS["G0"],
            verdict="fail" if {"not_mainboard", "st_or_delisting_risk"} & set(risk_flags) else "pass",
            conclusion="符合沪深主板账户范围且未识别为 ST/退市标的。" if not ({"not_mainboard", "st_or_delisting_risk"} & set(risk_flags)) else "不符合账户准入或存在退市风险标识。",
            evidence={"symbol": symbol, "name": name, "risk_flags": risk_flags},
        ),
        ResearchGate(
            gate_key="G1", label=GATE_LABELS["G1"], verdict="pass" if exact_mapping and theme else "unknown",
            conclusion=(f"同花顺行业归属为{board.get('label')}，供应商主题标签为“{theme}”。" if exact_mapping and theme
                        else "缺少精确行业归属或主营主题标签，不能仅凭价格形态判断公司身份。"),
            evidence={"industry": board.get("label"), "sector_key": board.get("sector_key"), "theme": theme,
                      "source": "longhuvip_composite"},
        ),
        ResearchGate(
            gate_key="G2", label=GATE_LABELS["G2"],
            verdict="pass" if valuation_pass and not legacy_recent_rejected else "advisory",
            conclusion=(f"当前截面 PE {pe:.2f} 倍、PB {pb:.2f} 倍，未触发短线估值硬否决。"
                        if valuation_pass and not legacy_recent_rejected
                        else f"当前截面 PE={pe}、PB={pb}，或近期既有公司研究已否决；该项仅作风险提示，不能解释为价值低估。"),
            evidence={"pe": pe, "pb": pb, "legacy_recent_rejected": legacy_recent_rejected,
                      "policy": "short-term advisory unless market eligibility fails"},
        ),
        ResearchGate(
            gate_key="G3", label=GATE_LABELS["G3"],
            verdict="pass" if board_net is not None and board_net > 0 and (board_percentile is None or board_percentile >= 0.45) else "fail",
            conclusion=(f"所属板块当日净流入 {board_net:,.0f} 元，板块催化得到资金截面确认。"
                        if board_net is not None and board_net > 0 and (board_percentile is None or board_percentile >= 0.45)
                        else "所属板块资金没有形成正向确认，不能把个股形态单独当成催化。"),
            evidence={"board_net_amount": board_net, "board_flow_percentile": board_percentile,
                      "industry": board.get("label")},
        ),
        ResearchGate(
            gate_key="G4", label=GATE_LABELS["G4"], verdict="pass" if exact_mapping and theme else "unknown",
            conclusion=(f"供应商行业成分与主题标签交叉指向“{board.get('label')} / {theme}”；"
                        "这只确认短线方向暴露，不等同于公司利润受益核验。"
                        if exact_mapping and theme else "尚未形成公司与当期方向之间的精确映射。"),
            evidence={"exact_member_mapping": exact_mapping, "theme": theme,
                      "boundary": "market-structure exposure; not a forecast of profit contribution"},
        ),
        ResearchGate(
            gate_key="G5", label=GATE_LABELS["G5"],
            verdict="pass" if g5_pass else "fail",
            conclusion=(f"成交额 {amount / 100_000_000:.2f} 亿元、换手率 {turnover:.2f}%，价格触发和失效位可执行。"
                        if g5_pass
                        else f"成交额={None if amount is None else round(amount / 100_000_000, 2)}亿元、"
                             f"换手率={turnover}%、量比={volume_ratio}；未达到短线流动性/活跃度执行门槛。"),
            evidence={"amount": amount, "turnover_rate": turnover, "volume_ratio": volume_ratio,
                      "main_net_amount": main_net, "geometry": geometry},
        ),
    ]
    gates.append(independent_downside_gate(geometry, risk_flags))
    gates.append(ResearchGate(
        gate_key="G7", label=GATE_LABELS["G7"], verdict="pass" if geometry else "unknown",
        conclusion=(f"入场/持有参考 {geometry['entry_lower']:.2f}—{geometry['entry_upper']:.2f}，"
                    f"失效位 {geometry['stop']:.2f}，目标 {geometry['target1']:.2f}/{geometry['target2']:.2f}。"
                    if geometry else "未能生成完整的入场、失效位、目标和仓位计划。"),
        evidence={"geometry": geometry, "holding": holding},
    ))
    status = terminal_status(gates)
    source_run_id = str(evidence.get("run_id")) if evidence.get("run_id") else None
    evidence_refs = [
        f"canonical-bars:{symbol}:{as_of_date}",
        f"longhu-daily-basic:{symbol}:{as_of_date}",
        f"longhu-main-net:{symbol}:{as_of_date}",
        f"longhu-industry:{board.get('sector_key')}:{as_of_date}",
    ]
    if isinstance(legacy, dict) and legacy.get("id") is not None:
        evidence_refs.append(f"legacy-research-run:{legacy['id']}")
    role = "holding" if holding else "candidate"
    source_identity = (
        str(evidence.get("portfolio_snapshot_id") or "no-snapshot")
        if holding else str(source_run_id or "no-candidate-run")
    )
    conclusion = (
        "短线市场结构研究通过：仅在量价和板块条件同时触发时执行；不扩张为长期价值结论。" if status == "passed"
        else "研究否决：保留证据供复盘；新买不得进入可执行列表。" if status == "rejected"
        else "证据不完整：不会生成新买计划。"
    )
    return DecisionResearchDossier(
        dossier_key=f"{RESEARCH_MODEL_VERSION}:{as_of_date}:{role}:{symbol}:{source_identity}",
        as_of_date=as_of_date, symbol=symbol, name=name, strategy_family=STRATEGY_FAMILY,
        model_version=RESEARCH_MODEL_VERSION, status=status, conclusion=conclusion,
        source_candidate_run_id=source_run_id,
        source_candidate_rank=int(evidence["rank"]) if evidence.get("rank") is not None else None,
        evidence_snapshot={
            "role": role, "geometry": geometry, "amount": amount, "turnover_rate": turnover,
            "volume_ratio": volume_ratio, "main_net_amount": main_net, "pe": pe, "pb": pb,
            "board": board, "theme": theme,
            "position": position or {},
            "legacy_terminal_research": ({"id": legacy.get("id"), "status": legacy.get("status"),
                                           "as_of": legacy.get("as_of")} if isinstance(legacy, dict) else None),
        },
        evidence_refs=evidence_refs, gates=gates,
    )


def _holding_plan(dossier: DecisionResearchDossier, position: dict[str, Any], as_of_at: datetime) -> PersonalTradePlanInput:
    geometry = dossier.evidence_snapshot["geometry"]
    weight = _float(position.get("position_weight_pct")) or 0.0
    rejected = dossier.status == "rejected"
    concentrated = weight > 25
    action = "reduce_on_trigger" if rejected or concentrated else "observe"
    # A risk-warning level must sit strictly between the current close and the
    # hard stop.  Reusing SMA20 could place the warning below the stop and make
    # the reduce branch unreachable.
    risk_warning = geometry["stop"] + (geometry["close"] - geometry["stop"]) * 0.45
    return PersonalTradePlanInput(
        plan_key=f"{RESEARCH_MODEL_VERSION}:{dossier.as_of_date}:holding:{dossier.symbol}:"
                 f"{sha256(dossier.dossier_key.encode()).hexdigest()[:16]}",
        plan_kind="holding", symbol=dossier.symbol, name=dossier.name,
        as_of_at=as_of_at, valid_until=as_of_at + timedelta(days=4), action=action,
        add_trigger=None,
        reduce_trigger=(
            f"跌破 {risk_warning:.2f} 且 10 分钟不能收复，同时所属板块转弱时先减三分之一；"
            f"反弹到 {geometry['resistance']:.2f} 附近但成交额不能继续放大时再减三分之一。"
        ),
        exit_trigger=(
            f"有效跌破 {geometry['stop']:.2f} 后退出剩余短线仓位；不因成本价或回本执念延后。"
        ),
        stop_price=_price(geometry["stop"]),
        target_prices=[_price(geometry["target1"]), _price(geometry["target2"])],
        max_position_pct=HOLDING_SHORT_TERM_CAP_PCT,
        rationale=[
            dossier.conclusion,
            (f"当前单票仓位约 {weight:.2f}%，超过 25% 的短线集中度控制线，优先在反弹中降低集中风险。"
             if concentrated else f"当前单票仓位约 {weight:.2f}%，未触发集中度强制降仓线。"),
            f"最新成交额 {(_float(dossier.evidence_snapshot.get('amount')) or 0) / 100_000_000:.2f} 亿元，"
            f"当日大单口径净额 {(_float(dossier.evidence_snapshot.get('main_net_amount')) or 0) / 10_000:.0f} 万元。",
            "持仓计划与新买推荐分开生成；持仓风险控制不会阻断独立候选扫描。",
        ],
        evidence_refs=[f"decision-research:{dossier.dossier_key}", *dossier.evidence_refs],
        risk_flags=[gate.conclusion for gate in dossier.gates if gate.verdict in {"fail", "advisory"}],
        metadata={"research_status": dossier.status, "strategy_family": dossier.strategy_family,
                  "source": "decision_research_closure", "current_position_weight_pct": weight,
                  "risk_warning_price": round(risk_warning, 4)},
    )


def _new_buy_plan(dossier: DecisionResearchDossier, as_of_at: datetime) -> PersonalTradePlanInput:
    geometry = dossier.evidence_snapshot["geometry"]
    amount = _float(dossier.evidence_snapshot.get("amount")) or 0
    max_position = Decimal("10") if amount >= 1_000_000_000 else Decimal("8")
    return PersonalTradePlanInput(
        plan_key=f"{RESEARCH_MODEL_VERSION}:{dossier.as_of_date}:new-buy:{dossier.symbol}:"
                 f"{sha256(dossier.dossier_key.encode()).hexdigest()[:16]}",
        plan_kind="new_buy", symbol=dossier.symbol, name=dossier.name,
        as_of_at=as_of_at, valid_until=as_of_at + timedelta(days=4), action="buy_on_trigger",
        entry_zone=PriceZone(lower=_price(geometry["entry_lower"]), upper=_price(geometry["entry_upper"])),
        add_trigger=(
            f"价格站上 {geometry['entry_lower']:.2f} 后，至少连续 10 分钟不跌回突破位，"
            "且所属板块资金仍为正、个股成交额不低于同时间段近五日均值时，分两笔建仓。"
        ),
        reduce_trigger=(
            f"冲击 {geometry['target1']:.2f} 但量价背离时先兑现一半；"
            f"到 {geometry['target2']:.2f} 附近不再追求满额收益。"
        ),
        exit_trigger=(
            f"买入后跌破 {geometry['stop']:.2f} 退出；若突破当天所属板块转为明显净流出，也取消未成交计划。"
        ),
        stop_price=_price(geometry["stop"]),
        target_prices=[_price(geometry["target1"]), _price(geometry["target2"])],
        max_position_pct=max_position,
        rationale=[
            dossier.conclusion,
            f"成交额 {amount / 100_000_000:.2f} 亿元，具备短线执行流动性。",
            f"第一目标的收益风险比约 {geometry['reward_risk_1']:.2f}，只在触发条件成立后买入。",
        ],
        evidence_refs=[f"decision-research:{dossier.dossier_key}", *dossier.evidence_refs],
        risk_flags=[gate.conclusion for gate in dossier.gates if gate.verdict == "advisory"],
        metadata={"research_status": dossier.status, "strategy_family": dossier.strategy_family,
                  "source_candidate_rank": dossier.source_candidate_rank,
                  "source": "decision_research_closure"},
    )


def refresh_decision_research_and_plans(
    database: Any, as_of_date: date, *, account_key: str = "citics-primary", candidate_limit: int = 12,
) -> dict[str, Any]:
    """Persist terminal research and plans for one settled close."""
    # The close itself is the evidence boundary.  Using a future evening time
    # made plans invisible between 15:00 and that arbitrary timestamp.
    as_of_at = datetime.combine(as_of_date, time(15, 10), tzinfo=SHANGHAI)
    with database.transaction() as connection:
        portfolio = latest_exact_portfolio(connection, account_key)
        holdings = list((portfolio or {}).get("positions") or [])
        candidate_rows = latest_candidate_evidence(connection, as_of_date, candidate_limit)
        holding_pairs: list[tuple[DecisionResearchDossier, dict[str, Any]]] = []
        for position in holdings:
            evidence = holding_evidence(connection, as_of_date, str(position["symbol"]))
            if evidence:
                evidence["symbol"] = position["symbol"]
                evidence["name"] = position["name"]
                evidence["portfolio_snapshot_id"] = (portfolio or {}).get("snapshot_id")
                holding_pairs.append((build_dossier(
                    evidence, as_of_date=as_of_date, holding=True, position=position,
                ), position))
        holding_dossiers = [item[0] for item in holding_pairs]
        candidate_dossiers = [
            build_dossier(row, as_of_date=as_of_date, holding=False) for row in candidate_rows
        ]
        dossier_receipts = [persist_dossier(connection, dossier) for dossier in [*holding_dossiers, *candidate_dossiers]]
        plan_receipts = []
        for dossier, position in holding_pairs:
            if dossier.evidence_snapshot.get("geometry"):
                plan_receipts.append(persist_trade_plan(connection, _holding_plan(dossier, position, as_of_at)))
        passed_candidates = [dossier for dossier in candidate_dossiers if dossier.status == "passed"]
        for dossier in passed_candidates:
            plan_receipts.append(persist_trade_plan(connection, _new_buy_plan(dossier, as_of_at)))
    return {
        "status": "completed",
        "as_of_date": str(as_of_date),
        "portfolio_observed_at": (portfolio or {}).get("observed_at"),
        "holding_dossiers": len(holding_dossiers),
        "candidate_dossiers": len(candidate_dossiers),
        "passed_candidates": len(passed_candidates),
        "rejected_candidates": sum(dossier.status == "rejected" for dossier in candidate_dossiers),
        "incomplete_candidates": sum(dossier.status == "incomplete" for dossier in candidate_dossiers),
        "trade_plans": len(plan_receipts),
        "dossier_receipts": dossier_receipts,
        "plan_receipts": plan_receipts,
        "boundary": "human_decision_support_only; no broker order path",
    }


__all__ = [
    "HOLDING_SHORT_TERM_CAP_PCT", "STRATEGY_FAMILY", "build_dossier", "independent_downside_gate",
    "refresh_decision_research_and_plans",
]
