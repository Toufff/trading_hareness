"""Deterministic pre-open and live-opening acceptance for intraday advisory.

This module deliberately has no recovery or scheduling side effects.  It turns
three existing read surfaces plus the persisted SSE calendar into one verdict;
the Windows runner owns the single bounded restart and Feishu delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


READY = "ready"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class OpeningGuardVerdict:
    stage: str
    status: str
    checked_at: datetime
    checks: tuple[dict[str, Any], ...]
    recovery_attempted: bool = False
    calendar_reason: str = ""

    @property
    def failed_checks(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for item in self.checks if not item["passed"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "checked_at": self.checked_at.isoformat(),
            "recovery_attempted": self.recovery_attempted,
            "calendar_reason": self.calendar_reason,
            "checks": list(self.checks),
            "failed_checks": [item["name"] for item in self.failed_checks],
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _instant(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _instant(value)
    if parsed is None or now.tzinfo is None:
        return None
    return max(0.0, (now - parsed.astimezone(now.tzinfo)).total_seconds())


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": str(detail)[:300]}


def evaluate_opening_guard(
    *,
    stage: str,
    checked_at: datetime,
    calendar_open: bool | None,
    calendar_reason: str,
    health: Mapping[str, Any],
    advisory: Mapping[str, Any],
    discipline: Mapping[str, Any],
    recovery_attempted: bool = False,
) -> OpeningGuardVerdict:
    """Return a fail-closed verdict without performing any external action."""
    if stage not in {"preopen", "live"}:
        raise ValueError(f"unsupported opening guard stage: {stage}")
    if checked_at.tzinfo is None:
        raise ValueError("checked_at must be timezone-aware")
    if calendar_open is False:
        return OpeningGuardVerdict(stage, SKIPPED, checked_at, (), recovery_attempted, calendar_reason)

    checks: list[dict[str, Any]] = []
    checks.append(_check("trade_calendar", calendar_open is True,
                         calendar_reason or "SSE calendar unavailable"))
    checks.append(_check("api_health", health.get("status") == "ok",
                         f"status={health.get('status') or 'unavailable'}"))

    for key in ("database_pool", "async_database_pool"):
        pool = _mapping(health.get(key))
        healthy = bool(pool.get("open")) and int(pool.get("waiting") or 0) == 0
        checks.append(_check(key, healthy,
                             f"open={bool(pool.get('open'))}; waiting={int(pool.get('waiting') or 0)}"))

    loops = _mapping(health.get("runtime_loops"))
    for name in ("intraday_advisory", "discipline_alerts"):
        loop = _mapping(loops.get(name))
        heartbeat_age = _age_seconds(loop.get("lease_heartbeat_at"), checked_at)
        healthy = (loop.get("state") == "running" and not loop.get("last_error") and
                   heartbeat_age is not None and heartbeat_age <= 180)
        detail = (f"state={loop.get('state') or 'missing'}; heartbeat_age="
                  f"{round(heartbeat_age, 1) if heartbeat_age is not None else 'missing'}s; "
                  f"error={loop.get('last_error') or 'none'}")
        checks.append(_check(f"runtime_loop:{name}", healthy, detail))

    advisory_runtime = _mapping(advisory.get("runtime"))
    advisory_details = _mapping(advisory_runtime.get("details"))
    blockers = advisory_details.get("blockers") if isinstance(advisory_details.get("blockers"), list) else []
    tick_age = _age_seconds(advisory_runtime.get("last_tick_at"), checked_at)
    checks.extend([
        _check("advisory_enabled", bool(advisory.get("enabled")),
               f"enabled={bool(advisory.get('enabled'))}"),
        _check("feishu_transport", bool(advisory.get("transport_configured")) and
               bool(discipline.get("transport_configured")),
               (f"advisory={bool(advisory.get('transport_configured'))}; "
                f"discipline={bool(discipline.get('transport_configured'))}")),
        _check("monitor_scope", int(advisory_runtime.get("scope_size") or 0) > 0 and not blockers,
               f"scope_size={int(advisory_runtime.get('scope_size') or 0)}; blockers={blockers}"),
        _check("advisory_tick", tick_age is not None and tick_age <= 15 and
               not advisory_runtime.get("last_error"),
               (f"tick_age={round(tick_age, 1) if tick_age is not None else 'missing'}s; "
                f"error={advisory_runtime.get('last_error') or 'none'}")),
    ])

    discipline_runtime = _mapping(discipline.get("runtime"))
    discipline_age = _age_seconds(discipline_runtime.get("last_completed_at"), checked_at)
    checks.append(_check("discipline_tick", discipline_age is not None and discipline_age <= 75 and
                         not discipline_runtime.get("last_error"),
                         (f"tick_age={round(discipline_age, 1) if discipline_age is not None else 'missing'}s; "
                          f"error={discipline_runtime.get('last_error') or 'none'}")))

    if stage == "live":
        quote = _mapping(advisory_details.get("quote_evidence"))
        success_age = _age_seconds(quote.get("success_at"), checked_at)
        received = int(quote.get("received") or 0)
        fresh = int(quote.get("fresh") or 0)
        checks.append(_check("live_quote_flow", success_age is not None and success_age <= 20 and
                             received > 0 and fresh > 0,
                             (f"success_age={round(success_age, 1) if success_age is not None else 'missing'}s; "
                              f"received={received}; fresh={fresh}")))

    status = READY if all(item["passed"] for item in checks) else FAILED
    return OpeningGuardVerdict(stage, status, checked_at, tuple(checks), recovery_attempted, calendar_reason)


def opening_guard_card(verdict: OpeningGuardVerdict) -> dict[str, Any]:
    """Render a compact native Feishu card; never include credentials."""
    stage_label = "盘前准备" if verdict.stage == "preopen" else "开盘实流"
    if verdict.status == READY:
        title = f"{stage_label}自检通过"
        template = "green"
        summary = "服务、监控范围、通知通道与运行循环均正常。"
        if verdict.stage == "live":
            summary = "已收到新鲜盘中行情，监控与分析链正式进入今日运行。"
    elif verdict.status == SKIPPED:
        title = f"{stage_label}自检跳过"
        template = "grey"
        summary = f"交易日历关闭：{verdict.calendar_reason}"
    else:
        title = f"{stage_label}自检失败"
        template = "red"
        names = "、".join(item["name"] for item in verdict.failed_checks) or "未知"
        summary = f"失败项：{names}。系统未把本次运行误报为正常。"
    recovery = "是" if verdict.recovery_attempted else "否"
    rows = "\n".join(
        f"- {'通过' if item['passed'] else '失败'} **{item['name']}**：{item['detail']}"
        for item in verdict.checks
    ) or "- 今日非交易日，无需启动盘中链路。"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{summary}**\n\n检查时间：{verdict.checked_at:%Y-%m-%d %H:%M:%S}\n自动恢复尝试：{recovery}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": rows}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "仅验证研究与提醒链路；不会连接券商下单，也不会执行交易。"}]},
        ],
    }


__all__ = [
    "FAILED", "READY", "SKIPPED", "OpeningGuardVerdict",
    "evaluate_opening_guard", "opening_guard_card",
]
