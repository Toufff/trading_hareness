"""Pure presentation helpers for intraday strategy notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


def intraday_alert_text(
    signal: dict[str, Any],
    watch: dict[str, Any],
    quote: dict[str, Any],
    minute_row: dict[str, Any] | None,
    decision_card_url: str | None = None,
) -> str:
    """Render an evidence-oriented Feishu message without side effects."""
    conditions = signal["conditions"]
    title = {
        "entry": "入场条件确认",
        "watch": "异常量能",
        "reduce": "减仓风险确认",
        "exit": "硬止损触发",
        "data_issue": "行情数据异常",
    }[signal["signal_type"]]
    if conditions.get("setup") == "eac_acceptance_confirmed" and signal["signal_type"] == "watch":
        title = "承接观察确认"
    if conditions.get("setup") == "countertrend_rebound_confirmed_plus_intraday_acceptance":
        title = "B浪反弹入场复核"
    if conditions.get("setup") == "countertrend_rebound_intraday_acceptance_failure":
        title = "B浪反弹承接失效"
    policy = conditions.get("policy_gate") if isinstance(conditions.get("policy_gate"), dict) else {}
    decision = conditions.get("decision_context") if isinstance(conditions.get("decision_context"), dict) else {}
    name = str(quote.get("name") or watch.get("label") or signal["symbol"])
    lines = [
        f"【盘中提醒｜{title}】",
        f"{name} {signal['symbol']}",
        f"信号观测时间（上海）：{_shanghai_time(signal.get('observed_at'))}",
        f"现价 {conditions.get('price', '—')}｜涨跌 {conditions.get('pct_change', '—')}%｜量比 {conditions.get('volume_ratio', '—')}｜换手 {conditions.get('turnover_rate', '—')}%",
        f"腾讯主力净流入指标 {conditions.get('main_net_inflow', '—')}（公开源估算）",
    ]
    if decision:
        lines.append(f"建议方向：{decision.get('action', '人工复核')}（系统不自动下单）")
        for index, reason in enumerate(decision.get("reasons") or [], start=1):
            lines.append(f"触发原因{index}：{reason}")
        lines.append(_probability_text(decision.get("probability")))
        invalidations = "；".join(str(item) for item in decision.get("invalidations") or [] if str(item))
        if invalidations:
            lines.append(f"失效/反证条件：{invalidations}")
    if conditions.get("setup") == "minute_price_volume_plus_sector_breadth":
        minute = conditions.get("minute_features") or {}
        peers = conditions.get("peer_context") or {}
        lines.append(
            f"板块共振：1分钟 {minute.get('return_1m_pct', '—')}%｜3分钟 {minute.get('return_3m_pct', '—')}%｜"
            f"分钟量 {minute.get('minute_volume_multiple', '—')} 倍｜同板块确认 {peers.get('confirming_peer_count', '—')}/{peers.get('available_peer_count', '—')}"
        )
    elif conditions.get("setup") == "leader_minute_burst":
        minute = conditions.get("minute_features") or {}
        lines.append(f"龙头首动：1分钟 {minute.get('return_1m_pct', '—')}%｜分钟量 {minute.get('minute_volume_multiple', '—')} 倍；等待同板块成分确认。")
    elif conditions.get("setup") == "eac_first_intraday_high":
        assessment = conditions.get("upside_research_assessment") or {}
        metrics = assessment.get("metrics") or {}
        profile = metrics.get("time_bucket_volume_profile") or {}
        state = "仅首动观察，等待承接" if conditions.get("eac_state") == "attention_only" else "首动满足，等待二次承接确认"
        surprise_text = (
            f"同刻量能惊喜 {metrics.get('time_bucket_volume_surprise')} 倍（{profile.get('sample_days')} 日基线）"
            if profile.get("status") == "ready"
            else f"同刻量能基线不足（{profile.get('sample_days', 0)} 日，仅观察）"
        )
        lines.append(
            f"EAC 首突破：3分钟 {metrics.get('return_3m_pct', '—')}%｜分钟量 {metrics.get('minute_volume_multiple', '—')} 倍｜"
            f"VWAP上方 {metrics.get('above_vwap_pct', '—')}%｜{surprise_text}｜时段 {metrics.get('session_window', '—')}；{state}。"
        )
    elif conditions.get("setup") == "eac_acceptance_confirmed":
        assessment = conditions.get("eac_acceptance_assessment") or conditions.get("upside_research_assessment") or {}
        metrics = assessment.get("metrics") or {}
        attention_text = "｜同刻基线不足，仅作承接观察" if conditions.get("eac_state") == "attention_only" else ""
        lines.append(
            f"EAC 承接确认：维持 {metrics.get('elapsed_seconds', '—')} 秒｜首动后保持 {metrics.get('retained_from_first_pct', '—')}%｜"
            f"相邻扫描 {metrics.get('scan_return_pct', '—')}%｜VWAP上方 {metrics.get('above_vwap_pct', '—')}%{attention_text}。"
        )
    if minute_row:
        lines.append(f"Tushare 最新分钟线：{minute_row.get('time') or minute_row.get('updated_at') or '已取得'}，收 {minute_row.get('close', '—')}")
    fast_confirmation = conditions.get("realtime_cross_check") or signal.get("fast_quote_confirmation") or {}
    if fast_confirmation.get("status") == "confirmed":
        lines.append(
            f"秒级价格交叉确认：Super GET {fast_confirmation.get('super_get_price', '—')}｜"
            f"腾讯 {fast_confirmation.get('tencent_price', '—')}｜偏差 {fast_confirmation.get('gap_pct', '—')}%。"
        )
    if decision_card_url:
        lines.append(f"决策卡（已保存证据）：{decision_card_url}")
    else:
        lines.append("决策卡：未配置可从飞书访问的研究台地址；可在本地研究台按代码打开。")
    if policy.get("decision") == "risk_alert_only":
        lines.append("执行约束：未确认可卖数量，此为风险告警，不表示当前可卖出。")
    elif policy.get("reason_codes"):
        lines.append(f"策略门禁：{policy.get('decision')}｜{','.join(str(item) for item in policy.get('reason_codes') or [])}")
    lines.append("仅为人工复核提醒，不构成交易指令；请结合盘口、板块、仓位和风险预算确认。")
    return "\n".join(lines)


def _probability_text(value: Any) -> str:
    profile = value if isinstance(value, dict) else {}
    estimate = profile.get("estimated_probability")
    if estimate is None:
        return (
            f"研究概率：暂不可估（匹配成熟样本 {int(profile.get('sample_rows') or 0)}；"
            "策略评分不是概率）。"
        )
    confidence = {
        "low": "低置信度", "preliminary": "初步", "validated": "达到样本门槛",
        "unavailable": "不可用",
    }.get(str(profile.get("confidence_tier") or ""), str(profile.get("confidence_tier") or "未分级"))
    raw = profile.get("raw_event_positive_rate")
    if raw is None:
        raw = profile.get("raw_positive_rate")
    raw_text = f"｜原始 {float(raw) * 100:.1f}%" if raw is not None else ""
    average = profile.get("average_directional_return")
    average_text = f"｜平均方向收益 {float(average) * 100:.2f}%" if average is not None else ""
    lower, upper = profile.get("confidence_interval_lower"), profile.get("confidence_interval_upper")
    interval_text = (
        f"｜不确定区间 {float(lower) * 100:.1f}%–{float(upper) * 100:.1f}%"
        if lower is not None and upper is not None else ""
    )
    return (
        f"研究概率（{profile.get('horizon', '—')}方向兑现）：{float(estimate) * 100:.1f}%（{confidence}）"
        f"{interval_text}{raw_text}{average_text}｜样本 {int(profile.get('sample_rows') or 0)} 条/"
        f"{int(profile.get('independent_trading_days') or 0)} 个独立交易日。"
    )


def _shanghai_time(value: Any) -> str:
    """Format the event observation time without pretending a send time is fresh."""
    if not isinstance(value, datetime):
        return "—"
    if value.tzinfo is None:
        return "—"
    return value.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def delivery_health_recovery_text(streak_count: int) -> str:
    """Render one operational receipt after a previously failed alert streak.

    An adapter outage cannot be announced through that same adapter.  This
    receipt is therefore deliberately sent only after a normal message proves
    the channel has recovered; it never presents a market signal as fresh.
    """
    return "\n".join([
        "【提醒通道恢复】",
        f"飞书策略提醒此前连续 {max(1, int(streak_count))} 次投递失败；当前通道已恢复。",
        "失败期间的信号已保留在本地 outbox/运行状态中，请在研究台核对未送达记录。",
        "此消息仅为运维回执，不构成交易或市场判断。",
    ])


def daily_strategy_summary_text(summary: dict[str, Any], dashboard_url: str | None = None) -> str:
    """Render one compact, evidence-only end-of-day review message."""
    signal_counts = summary.get("signal_counts") or {}
    outcome_counts = summary.get("outcome_counts") or {}
    post_close = summary.get("post_close") or {}
    readiness = summary.get("readiness") or {}
    learning = summary.get("offline_policy_learning") or {}
    daily_learning = learning.get("daily_review") if isinstance(learning.get("daily_review"), dict) else {}
    learning_gate = learning.get("validation_gate") if isinstance(learning.get("validation_gate"), dict) else {}
    candidates = post_close.get("candidates") or []
    candidate_text = "；".join(
        f"{item.get('name') or item.get('symbol')} {item.get('candidate_type')} {item.get('score')}"
        for item in candidates[:5]
    ) or "无"
    outcomes = "；".join(
        f"{key} 已结算 {value.get('matured', 0)} / 待结算 {value.get('pending', 0)}"
        for key, value in sorted(outcome_counts.items())
    ) or "尚无可结算盘中样本"
    blockers = "、".join(readiness.get("blockers") or []) or "无"
    lines = [
        f"【日终研究摘要｜{summary.get('exchange_date', '—')}】",
        f"盘中信号：已送达 {signal_counts.get('alerted', 0)}｜待确认 {signal_counts.get('confirmed', 0)}｜抑制去重 {signal_counts.get('suppressed', 0)}。",
        f"信号结算：{outcomes}。",
        f"盘后候选：{post_close.get('status', 'missing')}｜{candidate_text}。",
        f"数据门禁：{'通过' if readiness.get('decision_ready') else '未通过'}；阻塞项：{blockers}。",
        "策略学习：上下文动作回报离线复盘｜"
        f"当日已送达 {daily_learning.get('delivered_signals', 0)}，30m 已结算 {daily_learning.get('matured_30m_signals', 0)}｜"
        f"验证门禁 {learning_gate.get('status', 'accumulating')} "
        f"({learning_gate.get('matured_unique_signals', 0)}/{learning_gate.get('required_unique_signals', 200)} 信号，"
        f"{learning_gate.get('trading_days', 0)}/{learning_gate.get('required_trading_days', 60)} 交易日)；未自动改参。",
    ]
    if post_close.get("reason"):
        lines.append(f"盘后说明：{post_close['reason']}")
    if dashboard_url:
        lines.append(f"研究台：{dashboard_url}")
    lines.append("候选与盘中信号均为人工复核线索；当前统计样本不足，不展示胜率或自动交易结论。")
    return "\n".join(lines)
