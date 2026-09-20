from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.intraday_advisory.opening_guard import FAILED, READY, SKIPPED, evaluate_opening_guard, opening_guard_card


TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 22, 9, 32, tzinfo=TZ)


def _payloads(now: datetime = NOW):
    stamp = now.isoformat()
    health = {
        "status": "ok",
        "database_pool": {"open": True, "waiting": 0},
        "async_database_pool": {"open": True, "waiting": 0},
        "runtime_loops": {
            "intraday_advisory": {"state": "running", "last_error": None, "lease_heartbeat_at": stamp},
            "discipline_alerts": {"state": "running", "last_error": None, "lease_heartbeat_at": stamp},
        },
    }
    advisory = {
        "enabled": True,
        "transport_configured": True,
        "runtime": {
            "state": "idle", "scope_size": 7, "last_error": None, "last_tick_at": stamp,
            "details": {"blockers": [], "quote_evidence": {
                "attempt_at": stamp, "success_at": stamp, "received": 7, "fresh": 7,
            }},
        },
    }
    discipline = {"transport_configured": True, "runtime": {
        "last_completed_at": stamp, "last_error": None,
    }}
    return health, advisory, discipline


def _evaluate(stage: str, *, now: datetime = NOW, open_: bool | None = True,
              health=None, advisory=None, discipline=None):
    defaults = _payloads(now)
    return evaluate_opening_guard(
        stage=stage, checked_at=now, calendar_open=open_, calendar_reason="fixture",
        health=health or defaults[0], advisory=advisory or defaults[1],
        discipline=discipline or defaults[2],
    )


def test_preopen_accepts_live_loops_without_requiring_quote_evidence() -> None:
    health, advisory, discipline = _payloads()
    advisory["runtime"]["details"].pop("quote_evidence")
    verdict = _evaluate("preopen", health=health, advisory=advisory, discipline=discipline)
    assert verdict.status == READY
    assert not verdict.failed_checks


def test_live_acceptance_uses_persisted_quote_evidence_not_transient_runtime_state() -> None:
    verdict = _evaluate("live")
    assert verdict.status == READY
    assert any(item["name"] == "live_quote_flow" and item["passed"] for item in verdict.checks)


def test_live_acceptance_fails_closed_when_quote_success_is_stale() -> None:
    health, advisory, discipline = _payloads()
    advisory["runtime"]["details"]["quote_evidence"]["success_at"] = (NOW - timedelta(seconds=21)).isoformat()
    verdict = _evaluate("live", health=health, advisory=advisory, discipline=discipline)
    assert verdict.status == FAILED
    assert [item["name"] for item in verdict.failed_checks] == ["live_quote_flow"]
    assert opening_guard_card(verdict)["header"]["template"] == "red"


def test_closed_calendar_is_an_explicit_non_failure_skip() -> None:
    verdict = _evaluate("preopen", open_=False)
    assert verdict.status == SKIPPED
    assert not verdict.checks
