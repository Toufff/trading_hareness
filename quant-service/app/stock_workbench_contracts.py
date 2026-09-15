"""Declarative evidence and presentation contracts for the stock workbench.

The workbench is intentionally strategy-driven: a strategy declares the
evidence panels it needs and the UI renders that declaration.  Adding a new
strategy must not require another hand-written, always-on data dashboard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class WorkbenchStrategyContract:
    key: str
    label: str
    thesis: str
    required_panels: tuple[str, ...]
    optional_panels: tuple[str, ...]
    overlays: tuple[str, ...]
    metrics: tuple[str, ...]
    message_categories: tuple[str, ...]
    volume_confirmation: float
    horizon_sessions: int = 5


STRATEGY_CONTRACTS: tuple[WorkbenchStrategyContract, ...] = (
    WorkbenchStrategyContract(
        "user_tracking", "综合跟踪", "用户指定股票的价格、量能、资金、板块、估值、事件与风险是否形成一致结论",
        ("price", "volume", "vendor_flow", "turnover", "sector", "messages", "scenario"),
        ("technical", "market", "trade_plan"),
        ("ma5", "ma10", "ma20", "atr", "support", "resistance", "event_markers"),
        ("vendor_flow", "volume", "turnover", "macd", "rsi"),
        ("company", "catalyst", "capital", "risk", "regulatory", "market"), 1.00,
    ),
    WorkbenchStrategyContract(
        "accumulation", "潜伏观察", "资金积累与收盘区间收窄是否同时成立",
        ("price", "volume", "vendor_flow", "turnover", "scenario"),
        ("sector", "messages", "trade_plan"),
        ("ma5", "ma10", "range", "support", "resistance"),
        ("vendor_flow", "volume", "turnover"),
        ("capital", "risk", "company"), 1.05,
    ),
    WorkbenchStrategyContract(
        "expansion", "放量启动", "突破平台时，量能和板块是否共同确认",
        ("price", "volume", "turnover", "sector", "scenario"),
        ("vendor_flow", "messages", "trade_plan"),
        ("ma5", "ma20", "boll", "breakout", "atr"),
        ("volume", "turnover", "macd"),
        ("company", "catalyst", "market"), 1.30,
    ),
    WorkbenchStrategyContract(
        "pullback", "强势回踩", "强势结构回撤时，缩量承接是否仍在",
        ("price", "volume", "technical", "sector", "scenario"),
        ("vendor_flow", "messages", "trade_plan"),
        ("ma5", "ma10", "ma20", "atr", "support"),
        ("volume", "rsi", "macd"),
        ("risk", "company", "market"), 1.00,
    ),
    WorkbenchStrategyContract(
        "trend", "主线趋势", "个股相对强度和板块趋势能否延续",
        ("price", "volume", "technical", "sector", "scenario"),
        ("vendor_flow", "messages", "trade_plan"),
        ("ma5", "ma10", "ma20", "macd", "relative_strength"),
        ("macd", "rsi", "volume"),
        ("company", "catalyst", "market"), 1.10,
    ),
    WorkbenchStrategyContract(
        "event", "事件机会", "已核验事件是否被价格和成交行为确认",
        ("price", "volume", "messages", "scenario"),
        ("vendor_flow", "sector", "turnover", "trade_plan"),
        ("event_markers", "ma5", "ma20", "atr"),
        ("volume", "turnover", "vendor_flow"),
        ("company", "catalyst", "capital", "risk", "regulatory"), 1.20,
    ),
    WorkbenchStrategyContract(
        "relay", "情绪接力", "高热度结构是否仍有可成交承接，而非只剩加速",
        ("price", "volume", "turnover", "sector", "scenario"),
        ("vendor_flow", "messages", "trade_plan"),
        ("ma5", "vwap_proxy", "breakout", "event_markers"),
        ("turnover", "volume", "vendor_flow"),
        ("market", "capital", "risk", "regulatory"), 1.35,
    ),
    WorkbenchStrategyContract(
        "contraction", "波动收缩突破", "真实波幅与成交额先收缩，首次扩张时由板块共同确认",
        ("price", "volume", "technical", "sector", "scenario"),
        ("vendor_flow", "messages", "turnover", "trade_plan"),
        ("ma10", "ma20", "boll", "atr", "breakout"),
        ("volume", "turnover", "macd"),
        ("company", "catalyst", "market", "risk"), 1.20,
    ),
    WorkbenchStrategyContract(
        "rotation", "板块轮动初动", "行业广度、资金和中位个股是否连续改善",
        ("price", "volume", "sector", "vendor_flow", "scenario"),
        ("technical", "messages", "trade_plan"),
        ("ma5", "ma20", "relative_strength", "breakout"),
        ("vendor_flow", "volume", "turnover"),
        ("market", "catalyst", "company", "risk"), 1.05,
    ),
    WorkbenchStrategyContract(
        "reclaim", "恐慌回收", "排除已核验利空后，急跌或假跌破是否被真实承接收复",
        ("price", "volume", "technical", "sector", "messages", "scenario"),
        ("vendor_flow", "turnover", "trade_plan"),
        ("ma5", "ma10", "atr", "support", "event_markers"),
        ("volume", "vendor_flow", "rsi"),
        ("risk", "regulatory", "capital", "company", "market"), 1.10,
    ),
)


def strategy_catalog() -> list[dict[str, Any]]:
    return [asdict(contract) for contract in STRATEGY_CONTRACTS]


def strategy_contract(key: str) -> WorkbenchStrategyContract:
    for contract in STRATEGY_CONTRACTS:
        if contract.key == key:
            return contract
    raise ValueError(f"unknown stock workbench strategy: {key}")


__all__ = ["STRATEGY_CONTRACTS", "WorkbenchStrategyContract", "strategy_catalog", "strategy_contract"]
