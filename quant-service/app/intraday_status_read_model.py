"""Local-only assembly for the intraday services status board.

The API route deliberately injects its runtime dependencies.  This keeps the
frontend status view from importing the application singleton or initiating
market-provider work while still exposing session-aware freshness evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
import os
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .intraday_runtime_status import load_intraday_runtime_evidence


@dataclass(frozen=True)
class IntradayStatusDependencies:
    database: Any
    alert_max_attempts: int
    realtime_market_session: Callable[[], tuple[bool, str]]
    board_curve_session: Callable[[], tuple[bool, str]]
    high_frequency_window: Callable[[datetime], bool]
    scan_interval_seconds: Callable[[], int]
    provider_status: Callable[[], list[dict[str, Any]]]
    runtime_service_state: Callable[..., tuple[str, float | None]]
    json_safe: Callable[[Any], Any]
    super_get_fast_interval_seconds: Callable[[], float]
    super_get_fast_max_in_flight: Callable[[], int]
    fast_quote_retention_days: Callable[[], int]
    board_curve_enabled: Callable[[], bool]
    board_curve_retention_days: Callable[[], int]
    board_rotation_retention_days: Callable[[], int]
    daily_summary_automation_enabled: Callable[[], bool]


def intraday_services_status_payload(deps: IntradayStatusDependencies) -> dict[str, Any]:
    """Build the local decision-path status payload without provider I/O."""
    observed_at = datetime.now(timezone.utc)
    local_now = observed_at.astimezone(ZoneInfo("Asia/Shanghai"))
    session_active, session_reason = deps.realtime_market_session()
    board_session_active, board_session_reason = deps.board_curve_session()
    special_window = deps.high_frequency_window(local_now)
    normal_interval = deps.scan_interval_seconds()
    configs = {item["name"]: item for item in deps.provider_status()}
    super_configured = bool((configs.get("super_get") or {}).get("configured"))
    alert_configured = bool((os.getenv("QUANT_ALERT_WEBHOOK_URL") or "").strip()
                            and (os.getenv("QUANT_ALERT_WEBHOOK_TOKEN") or "").strip())
    evidence = load_intraday_runtime_evidence(deps.database, deps.alert_max_attempts)
    health_rows = evidence["health_rows"]
    quote_rows = evidence["quote_rows"]
    raw_rows = evidence["raw_rows"]
    minute_profile = dict(evidence["minute_profile"] or {})
    latest_scan = evidence["latest_scan"]
    latest_completed_scan = evidence["latest_completed_scan"]
    latest_board = evidence["latest_board"]
    latest_board_curve = evidence["latest_board_curve"]
    latest_delivery = evidence["latest_delivery"]
    delivery_history = evidence["delivery_history"]
    pending_delivery_count = evidence["pending_delivery_count"]
    pending_rotation_delivery_count = evidence["pending_rotation_delivery_count"]
    latest_daily_summary = evidence["latest_daily_summary"]
    latest_health_event = evidence["latest_health_event"]
    watch_row = evidence["watch_row"]
    health = {(str(row["provider_key"]), str(row["capability"])): dict(row) for row in health_rows}
    quotes = {str(row["source_name"]): dict(row) for row in quote_rows}
    raw = {str(row["api_name"]): dict(row) for row in raw_rows}

    def most_recent_health(keys: tuple[str, ...], capabilities: tuple[str, ...]) -> dict[str, Any]:
        candidates = [health[(key, capability)] for key in keys for capability in capabilities if (key, capability) in health]
        return max(candidates, key=lambda row: row.get("updated_at") or datetime.min.replace(tzinfo=timezone.utc), default={})

    def runtime_item(*, key: str, label: str, role: str, configured: bool, expected_active: bool,
                     last_observed_at: datetime | None, max_age_seconds: float | None, cadence: str,
                     health_row: dict[str, Any] | None = None, details: dict[str, Any] | None = None,
                     startup_grace_seconds: float = 60.0) -> dict[str, Any]:
        state, age_seconds = deps.runtime_service_state(
            configured=configured, expected_active=expected_active, last_observed_at=last_observed_at,
            observed_at=observed_at, max_age_seconds=max_age_seconds, startup_grace_seconds=startup_grace_seconds,
        )
        row = health_row or {}
        return {
            "key": key, "label": label, "role": role, "state": state, "configured": configured,
            "expected_active": expected_active, "cadence": cadence, "max_age_seconds": max_age_seconds,
            "last_observed_at": last_observed_at, "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "last_success_at": row.get("last_success_at"), "last_failure_at": row.get("last_failure_at"),
            "last_error": row.get("last_error"), "last_latency_ms": row.get("last_latency_ms"),
            "last_row_count": row.get("last_row_count"), "consecutive_failures": row.get("consecutive_failures", 0),
            "circuit_open_until": row.get("circuit_open_until"), "details": details or {},
        }

    scan_observed_at = latest_completed_scan["observed_at"] if latest_completed_scan else None
    tencent_quote = quotes.get("tencent_free", {})
    tencent_health = most_recent_health(("tencent_free",), ("realtime_quote",))
    order_book_quote = quotes.get("tencent_order_book", {})
    order_book_health = most_recent_health(("tencent_free",), ("order_book_quote",))
    fast_quote = quotes.get("tushare_super_get_rt_k", {})
    rt_k_raw = raw.get("rt_k", {})
    fast_observed_at = fast_quote.get("last_observed_at") or rt_k_raw.get("last_observed_at")
    fast_health = most_recent_health(("tushare_super_get", "tushare_super"), ("realtime_quote", "rt_k"))
    rt_min = raw.get("rt_min", {})
    rt_min_health = most_recent_health(("tushare_super_get", "tushare_super"), ("rt_min",))
    board_expected_age = 90.0 if deps.board_curve_enabled() else 90.0 if special_window else 360.0
    close_profile_active = session_active and time(14, 55) <= local_now.time() < time(15, 0)
    items = [
        runtime_item(
            key="strategy_scheduler", label="盘中策略调度器", role="观察池扫描、信号确认与去重",
            configured=normal_interval >= 30, expected_active=session_active, last_observed_at=scan_observed_at,
            max_age_seconds=25.0 if special_window else max(45.0, normal_interval * 1.8),
            cadence="特别窗口 10 秒；其他连续竞价 30 秒",
            details={"latest_run": deps.json_safe(dict(latest_scan)) if latest_scan else None,
                     "enabled_watch_count": int(watch_row["enabled"] or 0)},
        ),
        runtime_item(
            key="tencent_realtime", label="腾讯全 A 实时行情", role="观察池全覆盖报价、量比、换手与主力流",
            configured=True, expected_active=session_active,
            last_observed_at=tencent_quote.get("last_observed_at") or scan_observed_at,
            max_age_seconds=25.0 if special_window else max(45.0, normal_interval * 1.8),
            cadence="特别窗口 10 秒；其他连续竞价 30 秒", health_row=tencent_health,
            details={"persisted_rows": int(tencent_quote.get("rows") or 0)},
        ),
        runtime_item(
            key="tencent_order_book", label="腾讯观察池五档盘口", role="QI、OFI 近似、内外盘差分与区间 VWAP 的研究证据",
            configured=os.getenv("INTRADAY_ORDER_BOOK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            expected_active=session_active, last_observed_at=order_book_quote.get("last_observed_at"), max_age_seconds=12.0,
            cadence="显式观察池批量每 3 秒", health_row=order_book_health,
            details={"persisted_rows": int(order_book_quote.get("rows") or 0),
                     "enabled_watch_count": int(watch_row["enabled"] or 0),
                     "max_symbols": 20,
                     "uncovered_watch_count": max(0, int(watch_row["enabled"] or 0) - 20),
                     "scope": "最多 20 只观察池；特征仅观测，不改变触发阈值"},
            startup_grace_seconds=20.0,
        ),
        runtime_item(
            key="super_get_rt_k", label="Super GET 秒级 rt_k", role="与腾讯现价交叉确认，冲突时阻止直接推送",
            configured=super_configured, expected_active=session_active and special_window,
            last_observed_at=fast_observed_at, max_age_seconds=30.0, cadence="特别窗口全局每秒启动 1 次",
            health_row=fast_health, details={"persisted_fast_rows": int(fast_quote.get("rows") or 0),
                                             "rotation_symbols": int(watch_row["enabled"] or 0),
                                             "max_in_flight": deps.super_get_fast_max_in_flight()},
            startup_grace_seconds=45.0,
        ),
        runtime_item(
            key="super_get_rt_min", label="Super GET 分钟 rt_min", role="分钟量价、VWAP 与首动指标验证",
            configured=super_configured, expected_active=session_active and not special_window,
            last_observed_at=rt_min.get("last_observed_at"), max_age_seconds=90.0,
            cadence="普通连续竞价每 30 秒轮转最多 4 只", health_row=rt_min_health,
            details={"stored_raw_rows": int(rt_min.get("rows") or 0)}, startup_grace_seconds=90.0,
        ),
        runtime_item(
            key="tencent_minute_profile", label="腾讯观察池分钟剖面", role="盘末保存全部显式观察池的同刻量能基线",
            configured=os.getenv("INTRADAY_MINUTE_PROFILE_CAPTURE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            expected_active=close_profile_active,
            last_observed_at=minute_profile.get("last_observed_at"), max_age_seconds=180.0,
            cadence="交易日 14:55–15:00；默认最多 40 只",
            details={"persisted_rows": int(minute_profile.get("rows") or 0),
                     "latest_trading_date": str(minute_profile.get("latest_trading_date") or "") or None,
                     "source": "tencent_intraday_minutes",
                     "feature_scope": "分钟收盘与累计量额；不伪装为真实分钟 OHLC"}, startup_grace_seconds=90.0,
        ),
        runtime_item(
            key="eastmoney_board_flow", label="东财板块资金流", role="行业/概念每分钟轮动检测；完整 Top10 报告保持独立五分钟节流",
            configured=deps.board_curve_enabled(), expected_active=board_session_active,
            last_observed_at=(latest_board_curve["observed_at"] if latest_board_curve else
                              latest_board["observed_at"] if latest_board else None),
            max_age_seconds=board_expected_age, cadence="上交所观察时段 09:20 起每 60 秒追加曲线",
            details={"latest_curve_status": latest_board_curve["status"] if latest_board_curve else None,
                     "curve_coverage": deps.json_safe(latest_board_curve["coverage"]) if latest_board_curve else None,
                     "latest_strategy_report_status": latest_board["status"] if latest_board else None,
                     "session_reason": board_session_reason,
                     "retention_days": deps.board_curve_retention_days(),
                     "rotation_retention_days": deps.board_rotation_retention_days()},
            startup_grace_seconds=90.0,
        ),
    ]
    latest_board_feed = latest_board_curve or latest_board
    if board_session_active and latest_board_feed and latest_board_feed["status"] not in {"completed", "partial"}:
        board_summary = latest_board["summary"] if latest_board and isinstance(latest_board["summary"], dict) else {}
        items[-1]["state"] = "degraded"
        items[-1]["last_error"] = str(board_summary.get("reason") or f"latest board report status is {latest_board['status']}")[:500]
    delivery = dict(latest_delivery) if latest_delivery else {}
    health_event = dict(latest_health_event) if latest_health_event else {}
    consecutive_delivery_failures = 0
    for row in delivery_history:
        if row["status"] == "failed":
            consecutive_delivery_failures += 1
        elif row["status"] != "pending":
            break
    unresolved_delivery_outage = health_event.get("event_type") == "failure_streak" and health_event.get("delivery_status") == "observed"
    feishu_state = "disabled" if not alert_configured else "degraded" if (consecutive_delivery_failures or unresolved_delivery_outage) else "ready"
    items.append({
        "key": "feishu_alert", "label": "飞书策略提醒", "role": "仅投递显式观察池中已确认的个股策略信号",
        "state": feishu_state, "configured": alert_configured, "expected_active": True, "cadence": "事件触发",
        "max_age_seconds": None, "last_observed_at": delivery.get("sent_at") or delivery.get("created_at"),
        "age_seconds": round(max(0.0, (observed_at - (delivery.get("sent_at") or delivery.get("created_at"))).total_seconds()), 1)
                       if delivery.get("sent_at") or delivery.get("created_at") else None,
        "last_success_at": delivery.get("sent_at"), "last_failure_at": delivery.get("created_at") if delivery.get("status") == "failed" else None,
        "last_error": delivery.get("error_message"), "last_latency_ms": None, "last_row_count": None,
        "consecutive_failures": consecutive_delivery_failures, "circuit_open_until": None,
        "details": {"latest_delivery_kind": delivery.get("kind"), "latest_delivery_status": delivery.get("status"),
                    "pending_retry_count": int(pending_delivery_count or 0) + int(pending_rotation_delivery_count or 0),
                    "pending_rotation_retry_count": int(pending_rotation_delivery_count or 0),
                    "notification_scope": "watched_stock_signals_only",
                    "meta_alert_state": (
                        "out_of_band_attention_required" if unresolved_delivery_outage else
                        "recovery_receipt_sent" if health_event.get("event_type") == "recovered" and health_event.get("delivery_status") == "sent" else
                        "recovery_receipt_pending" if health_event.get("event_type") == "recovered" else "normal"
                    ), "latest_health_event": deps.json_safe(health_event) if health_event else None,
                    "adapter_http_checked_by_dashboard": True},
    })
    summary_delivery = dict(latest_daily_summary) if latest_daily_summary else {}
    summary_expected = deps.daily_summary_automation_enabled() and time(19, 15) <= local_now.time() < time(19, 30)
    summary_item = runtime_item(
        key="daily_strategy_summary", label="日终研究摘要", role="保存盘中结算、策略学习、盘后候选与数据门禁到研究台",
        configured=deps.daily_summary_automation_enabled(), expected_active=summary_expected,
        last_observed_at=summary_delivery.get("sent_at") or summary_delivery.get("updated_at"), max_age_seconds=15 * 60.0,
        cadence="交易日 19:15–19:30；失败至多重试 3 次",
        details={"latest_exchange_date": str(summary_delivery.get("exchange_date") or "") or None,
                 "latest_delivery_status": summary_delivery.get("delivery_status"),
                 "attempt_count": int(summary_delivery.get("attempt_count") or 0),
                 "next_attempt_at": summary_delivery.get("next_attempt_at"), "read_only_market_evidence": True},
        startup_grace_seconds=90.0,
    )
    if not deps.daily_summary_automation_enabled():
        summary_item["state"] = "disabled"
        summary_item["last_error"] = "DAILY_SUMMARY_AUTOMATION_ENABLED is disabled"
    elif summary_delivery.get("delivery_status") == "failed":
        summary_item["state"] = "degraded"
        summary_item["last_error"] = summary_delivery.get("error_message") or "日终摘要最近一次投递失败"
    items.append(summary_item)
    items.append({
        "key": "primary_realtime", "label": "Tushare 主源实时", "role": "明确排除，不进入盘中决策",
        "state": "unavailable", "configured": bool((configs.get("primary") or {}).get("configured")),
        "expected_active": False, "cadence": "不调用", "max_age_seconds": None, "last_observed_at": None,
        "age_seconds": None, "last_success_at": None, "last_failure_at": None,
        "last_error": "主源不具备已验证实时能力", "last_latency_ms": None, "last_row_count": None,
        "consecutive_failures": 0, "circuit_open_until": None,
        "details": {"decision_eligible": False, "reason": "unavailable or unpurchased live-family routes"},
    })
    counts: dict[str, int] = {}
    for item in items:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "observed_at": observed_at, "timezone": "Asia/Shanghai", "session_active": session_active,
        "session_reason": session_reason, "special_window_active": special_window,
        "summary": {"states": counts, "enabled_watch_count": int(watch_row["enabled"] or 0),
                    "decision_path_degraded": any(item["state"] == "degraded" and item["expected_active"] for item in items)},
        "items": items,
    }


__all__ = ["IntradayStatusDependencies", "intraday_services_status_payload"]
