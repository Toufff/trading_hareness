"""Stable, secret-free repository context for maintenance agents."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CONTEXT_VERSION = "2026-08-22.v5"


def repository_agent_context() -> dict[str, Any]:
    return {
        "context_version": CONTEXT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "service_boundary": "research_only_no_orders",
        "time_zone": "Asia/Shanghai",
        "entrypoints": {
            "api_composition": "quant-service/app/main.py",
            "http_routers": "quant-service/app/routers/",
            "database_migrations": "quant-service/migrations/versions/",
            "frontend": "frontend/src/App.vue",
            "feishu_proxy": "feishu-adapter/index.mjs",
        },
        "module_map": {
            "security": "quant-service/app/security.py",
            "automation_runs": "quant-service/app/automation_run_repository.py",
            "remote_archive_sync": "quant-service/app/remote_archive_sync.py",
            "post_close_orchestrator": "quant-service/app/post_close_refresh.py",
            "post_close_stage_receipts": "quant-service/app/post_close_refresh.py + app/automation_run_repository.py",
            "akshare_probe": "quant-service/app/akshare_probe_service.py",
            "daily_strategy_summary": "quant-service/app/daily_strategy_summary_service.py",
            "daily_strategy_summary_scheduler": "quant-service/app/daily_strategy_summary_scheduler.py",
            "strategy_decision": "quant-service/app/strategy_decision_service.py",
            "strategy_review": "quant-service/app/strategy_review_service.py",
            "strategy_review_scheduler": "quant-service/app/strategy_review_scheduler.py",
            "board_research": "quant-service/app/board_research_service.py",
            "short_term_review": "quant-service/app/short_term_review.py",
            "analyst_review": "quant-service/app/analyst_market_review.py",
            "strategy_rules": "quant-service/app/*_rules.py and *_research.py",
            "api_routers": "quant-service/app/routers/",
            "network_resilience": "quant-service/app/network_health.py + app/runtime_tasks.py",
            "intraday_sector_report": "quant-service/app/intraday_sector_report_orchestrator.py + app/intraday_sector_report_service.py",
        },
        "contracts": {
            "openapi_source": "http://127.0.0.1:5681/openapi.json",
            "generated_frontend_types": "frontend/src/api/generated.ts",
            "contract_check": "node scripts/verify-api-contract.mjs",
            "type_check": "cd frontend && npm run api:check && npm run typecheck",
        },
        "operational_reads": {
            "health": "/health",
            "agent_context": "/api/v1/agent/context",
            "automation_runs": "/api/v1/automation/runs?task_key=...",
            "frontend_proxy_context": "/api/research/agent/context",
            "network_state": "/health -> network (passive outbound observation; no probe quota)",
        },
        "maintenance_sequence": [
            "read agent context and current automation receipt",
            "inspect router, repository, migration and existing tests",
            "make one bounded extraction or contract change",
            "run backend, adapter, frontend and diff verification",
            "verify the mounted OpenAPI and runtime health before handoff",
        ],
        "evidence_flow": ["raw", "canonical", "features", "signals", "outcomes"],
        "post_close_review": {
            "method": "short-term-review-v1",
            "steps": [
                "market_emotion: limit-up/down breadth and previous-limit premium",
                "ladder: highest board and consecutive-board gaps",
                "sector_structure: exact saved-member/quote coverage only",
                "capital_and_lhb: top-20 amount leaders and LHB net direction",
                "loss_effect: broad and concentrated drawdown flags",
                "wind_flags: bounded first-board, consecutive and repair samples",
                "next_session_plan: triggers and invalidations, research-only",
            ],
            "decision_eligible": False,
            "missing_data_policy": "fail_closed",
        },
        "research_boundaries": [
            "stated_at is replay evidence; strategy_available_at is eligibility time",
            "missing/stale/incomplete provider data fails closed",
            "replay_only outcomes never enter live weights",
            "regression and analyst effects remain live_effect=none until sample gates pass",
            "transport outages keep local loops alive, back off, fail closed, and retry the same durable cursor/run key after recovery",
        ],
        "verification": {
            "backend": "docker compose exec -T quant-research python -m unittest discover -s tests -q",
            "adapter": "node --test feishu-adapter/*.test.mjs",
            "frontend": "cd frontend && npm run typecheck && npm run build",
            "diff": "git diff --check",
        },
        "security": {
            "write_boundary": "X-Quant-Write-Key middleware",
            "secret_values_exposed": False,
            "tests": "quant-service/tests/test_p0_sql_and_auth.py",
        },
    }


__all__ = ["CONTEXT_VERSION", "repository_agent_context"]
