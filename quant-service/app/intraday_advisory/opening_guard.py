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
        index_age = _age_seconds(quote.get("index_success_at"), checked_at)
        received = int(quote.get("received") or 0)
        fresh = int(quote.get("fresh") or 0)
        index_fresh = int(quote.get("indices_fresh") or 0)
        checks.append(_check("live_quote_flow", success_age is not None and success_age <= 20 and
                             received > 0 and fresh > 0,
                             (f"success_age={round(success_age, 1) if success_age is not None else 'missing'}s; "
                              f"received={received}; fresh={fresh}")))
        checks.append(_check("live_index_flow", index_age is not None and index_age <= 30 and index_fresh >= 4,
                             (f"success_age={round(index_age, 1) if index_age is not None else 'missing'}s; "
                              f"fresh={index_fresh}")))

    status = READY if all(item["passed"] for item in checks) else FAILED
    return OpeningGuardVerdict(stage, status, checked_at, tuple(checks), recovery_attempted, calendar_reason)


def opening_guard_card(verdict: OpeningGuardVerdict) -> dict[str, Any]:
    """Render a compact native Feishu card; never include credentials."""
    if verdict.status == READY:
        title = "提醒服务已恢复"
        template = "blue"
        summary = "此前通知的服务故障已通过本次检查。"
        if verdict.stage == 'preopen':
            summary += "盘前检查不代表已验证开盘后的实时行情。"
    elif verdict.status == SKIPPED:
        title = "服务检查跳过"
        template = "grey"
        summary = f"交易日历关闭：{verdict.calendar_reason}"
    else:
        title = "服务异常｜盘中提醒可能不完整"
        template = "red"
        names = {item['name'] for item in verdict.failed_checks}
        impacts = []
        if names & {'live_quote_flow','live_index_flow','monitor_scope','advisory_tick','runtime_loop:intraday_advisory'}:
            impacts.append('行情变化提醒可能缺失或延迟')
        if names & {'discipline_tick','runtime_loop:discipline_alerts'}:
            impacts.append('纪律提醒可能延迟')
        if 'feishu_transport' in names:
            impacts.append('飞书通知通道异常')
        summary = '；'.join(impacts) or '研究提醒服务暂未通过可用性检查'
        summary += '。自动恢复后仍未通过检查，请暂时自行核对行情与原纪律条件。'
    recovery = "是" if verdict.recovery_attempted else "否"
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
             f"**{summary}**\n\n检查时间：{verdict.checked_at:%Y-%m-%d %H:%M:%S}\n自动恢复尝试：{recovery}"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content":
             "技术检查详情已写入后台运行日志。"}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "仅验证研究与提醒链路；不会连接券商下单，也不会执行交易。"}]},
        ],
    }


__all__ = [
    "FAILED", "READY", "SKIPPED", "OpeningGuardVerdict",
    "evaluate_opening_guard", "opening_guard_card",
]
