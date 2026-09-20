from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.post_close_strategy_service import run as run_post_close


def _lane_summary():
    lane = {
        "key": "expansion", "label": "放量启动", "purpose": "观察启动",
        "status": "completed", "total_matches": 0, "selected": [],
        "observation_list": [], "caution_list": [], "empty_reason": "没有匹配",
    }
    return {
        "as_of_date": "2026-09-20", "version": "test", "status": "completed",
        "coverage": {"universe": 1, "complete_history": 1, "verified_event_symbols": 0},
        "market": {}, "notice": "研究观察", "settings": {}, "sessions": ["2026-09-20"],
        "lanes": [lane], "company_reviews": [],
    }


def test_post_close_thesis_persistence_failure_is_visible_without_losing_main_report(monkeypatch):
    database, connection = MagicMock(), MagicMock()
    database.transaction.return_value.__enter__.return_value = connection
    latest, inserted = MagicMock(), MagicMock()
    latest.fetchone.return_value = {"trading_date": date(2026, 9, 20)}
    inserted.fetchone.return_value = {"run_id": "run-post-close"}
    connection.execute.side_effect = [latest, inserted, MagicMock(), MagicMock()]
    result = {
        "status": "completed", "as_of_date": "2026-09-20", "candidates": [],
        "screen_observations": [], "source_status": {}, "summary": {},
    }

    from app.trade_thesis import scan_adapter
    monkeypatch.setattr(scan_adapter, "refresh_from_run", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("persist")))
    payload = run_post_close(
        database, SimpleNamespace(as_of_date=date(2026, 9, 20), limit=20, minimum_full_market_symbols=1),
        model_version="hook-test", candidate_loader=lambda *_: result, json_safe=lambda value: value,
        lane_loader=lambda _day: _lane_summary(),
    )

    assert payload["status"] == "completed"
    thesis = payload["summary"]["trade_thesis"]
    assert thesis["status"] == "failed" and thesis["error_type"] == "RuntimeError"
    assert thesis["live_effect"] == "none" and thesis["decision_binding"] is False
    assert payload["summary"]["strategy_lanes"]["report_bundle"]["reports"]


def test_intraday_hook_runs_after_durable_readback_and_is_locally_failure_isolated():
    path = Path(__file__).resolve().parents[1] / "app" / "intraday_scan" / "runner.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    calls = [
        (node.lineno, ast.unparse(node.func))
        for node in ast.walk(run) if isinstance(node, ast.Call)
    ]
    save_line = next(line for line, name in calls if name == "repository.save")
    refresh_line = next(line for line, name in calls if name == "refresh_from_run")
    assert save_line < refresh_line
    refresh_try = next(
        node for node in ast.walk(run) if isinstance(node, ast.Try)
        and any(isinstance(call, ast.Call) and ast.unparse(call.func) == "refresh_from_run"
                for call in ast.walk(node))
    )
    assert refresh_try.handlers
    assert any("completed_thesis_partial" in ast.unparse(node) for node in ast.walk(refresh_try))


def test_both_production_hooks_import_the_real_scan_adapter_stage():
    app = Path(__file__).resolve().parents[1] / "app"
    for relative in ("post_close_strategy_service.py", "intraday_scan/runner.py"):
        source = (app / relative).read_text(encoding="utf-8")
        assert "from .trade_thesis.scan_adapter import refresh_from_run" in source or \
               "from ..trade_thesis.scan_adapter import refresh_from_run" in source
        assert "refresh_from_run(database" in source
