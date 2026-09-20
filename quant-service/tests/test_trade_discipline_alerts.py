from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.runtime_tasks import BackgroundTaskSpec
from app.trade_discipline.alerts_eligibility import AlertScope, load_alert_scope
from app.trade_discipline.alerts_evaluation import MinuteTapeRejected, validate_minute_tape
from app.trade_discipline.alerts_repository import persist_evaluation_transitions
from app.trade_discipline.alerts_runtime import DisciplineAlertRuntimeDependencies, run_discipline_alert_cycle
from app.trade_discipline.contracts import (
    Action, Confirm, Derivation, DisciplinePlan, Evaluation, Line, LineState, PositionRef, QualityCheck,
)


SH = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 21, 10, 1, 30, tzinfo=SH)


def alert_plan(*, basis: str = "minute", kind: str = "holding") -> DisciplinePlan:
    line = Line(
        kind="hard_stop" if kind == "holding" else "trigger", label="测试纪律线",
        metric="minute_close" if basis == "minute" else "daily_close", op="<=" if kind == "holding" else ">=",
        price=Decimal("10"), confirm=Confirm(bars=1, basis=basis),
        action=Action(type="exit_all" if kind == "holding" else "alert"),
        derivation=Derivation(rule_id="test", inputs={"level": 10}, formula="level"), priority=1,
    )
    return DisciplinePlan(
        plan_key=f"citics-primary:000001.SZ:2026-09-21:{kind}", run_id="run-alert-test",
        account_key="citics-primary", symbol="000001.SZ", name="测试股份", plan_kind=kind,
        stage="base_platform", template_key="test@v1", template_version="v1", as_of_at=AS_OF,
        trading_date=AS_OF.date(), valid_until=datetime(2026, 9, 25, 15, 0, tzinfo=SH),
        lines=[line], evidence_refs=["test"], quality=[QualityCheck(check_id="test", passed=True)],
        inputs_hash="a" * 64, generator_version="test-v1",
    )


def evaluation(state: str, *, basis: str = "minute") -> Evaluation:
    observed = datetime(2026, 9, 21, 10, 1, tzinfo=SH)
    return Evaluation(
        plan_id="plan-1", as_of_at=observed, trading_date=observed.date(), basis=basis,
        line_states=[LineState(kind="hard_stop", label="测试纪律线", state=state, basis=basis,
                               triggered_at=observed if state == "triggered" else None,
                               trigger_price=Decimal("9.9") if state == "triggered" else None)],
        inputs_hash=(state + "x" * 64)[:64],
    )


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _TransitionConnection:
    def __init__(self):
        self.states = {}
        self.events = {}
        self.deliveries = {}

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        if flat.startswith("SELECT plan_id,line_key,state"):
            row = self.states.get((params[0], params[1]))
            return _Result([row] if row else [])
        if flat.startswith("INSERT INTO quant.discipline_alert_line_states"):
            plan_id, key, index, kind, state, eval_id, suppressed, first, last = params
            identity = (plan_id, key)
            if identity in self.states:
                return _Result()
            self.states[identity] = {
                "plan_id": plan_id, "line_key": key, "line_index": index, "line_kind": kind,
                "state": state, "evaluation_id": eval_id, "baseline_suppressed": suppressed,
                "first_observed_at": first, "last_observed_at": last,
            }
            return _Result([{"state": state}])
        if flat.startswith("SELECT state,evaluation_id,baseline_suppressed"):
            row = self.states.get((params[0], params[1]))
            return _Result([row] if row else [])
        if flat.startswith("UPDATE quant.discipline_alert_line_states"):
            state, evaluation_id, last, plan_id, key = params
            self.states[(plan_id, key)].update(
                state=state, evaluation_id=evaluation_id, last_observed_at=last)
            return _Result()
        if flat.startswith("INSERT INTO quant.discipline_alert_events"):
            event_key = params[0]
            if event_key in self.events:
                return _Result()
            row = {"event_id": f"event-{len(self.events) + 1}", "event_key": event_key}
            self.events[event_key] = row
            return _Result([row])
        if flat.startswith("INSERT INTO quant.discipline_alert_deliveries"):
            event_id, text = params
            if event_id in self.deliveries:
                return _Result()
            row = {"delivery_id": f"delivery-{len(self.deliveries) + 1}", "message_text": text}
            self.deliveries[event_id] = row
            return _Result([row])
        raise AssertionError(flat)


class MinuteTapeValidationTests(unittest.TestCase):
    def test_longhu_observed_lunch_resume_at_1301_is_accepted(self):
        rows = [
            {"time": "1129", "close": 10, "is_complete": True},
            {"time": "1130", "close": 10, "is_complete": True},
            {"time": "1301", "close": 10.1, "is_complete": True},
        ]
        tape = validate_minute_tape(
            {"session_date": "2026-09-21", "rows": rows},
            datetime(2026, 9, 21, 13, 1, 30, tzinfo=SH),
        )
        self.assertEqual(tape.as_of.strftime("%H%M"), "1301")

    def test_an_arbitrary_lunch_gap_is_rejected(self):
        rows = [
            {"time": "1130", "close": 10, "is_complete": True},
            {"time": "1302", "close": 10.1, "is_complete": True},
        ]
        with self.assertRaisesRegex(MinuteTapeRejected, "minute_gap:1130->1302"):
            validate_minute_tape(
                {"session_date": "2026-09-21", "rows": rows},
                datetime(2026, 9, 21, 13, 2, 30, tzinfo=SH),
            )


class _ScopeConnection:
    def __init__(self, *, snapshot=True, recommendation=True, plans=()):
        self.snapshot = snapshot
        self.recommendation = recommendation
        self.plans = list(plans)

    def execute(self, sql, params=()):
        flat = " ".join(sql.split())
        if "FROM quant.broker_portfolio_snapshots" in flat:
            rows = [{"snapshot_id": "snapshot-1", "account_key": "citics-primary",
                     "observed_at": AS_OF, "verification": "verified_exact", "metadata": {}}]
            return _Result(rows if self.snapshot else [])
        if "FROM quant.broker_position_snapshots" in flat:
            return _Result([{"symbol": "000001.SZ"}])
        if "FROM quant.recommendation_pool_decisions" in flat:
            rows = [{"decision_id": "decision-1", "created_at": AS_OF,
                     "result": {"status": "ready", "sync_allowed": True,
                                "recommended": [{"symbol": "000002.SZ"}]}}]
            return _Result(rows if self.recommendation else [])
        if "FROM quant.discipline_plans" in flat:
            return _Result(self.plans)
        raise AssertionError(flat)


class EligibilityTests(unittest.TestCase):
    def test_scope_is_exact_intersection_of_snapshot_and_formal_decision_evidence(self):
        position = PositionRef(
            snapshot_id="snapshot-1", observed_at=AS_OF, quantity=100, sellable_quantity=100,
            average_cost=Decimal("10"), market_price=Decimal("10"), market_value=Decimal("1000"),
        )
        holding = alert_plan().model_copy(update={"position": position})
        prospective = alert_plan(kind="new_buy").model_copy(update={
            "symbol": "000002.SZ", "name": "正式推荐", "plan_key": "new-buy-000002",
            "evidence_refs": ["recommendation_decision:decision-1"],
        })
        connection = _ScopeConnection(plans=[
            {"plan_id": "holding-plan", "plan": holding},
            {"plan_id": "buy-plan", "plan": prospective},
        ])
        with patch("app.trade_discipline.alerts_eligibility.broker_freshness",
                   return_value={"current": True}), \
             patch("app.trade_discipline.alerts_eligibility.plan_from_row",
                   side_effect=lambda row: row["plan"]):
            scope = load_alert_scope(connection, account_key="citics-primary", as_of=AS_OF)
        self.assertEqual([plan_id for plan_id, _plan in scope.plans], ["holding-plan", "buy-plan"])
        self.assertEqual(scope.blockers, ())

    def test_stale_snapshot_and_wrong_recommendation_evidence_fail_closed(self):
        prospective = alert_plan(kind="new_buy").model_copy(update={
            "symbol": "000002.SZ", "plan_key": "new-buy-000002",
            "evidence_refs": ["recommendation_decision:old-decision"],
        })
        connection = _ScopeConnection(plans=[{"plan_id": "buy-plan", "plan": prospective}])
        with patch("app.trade_discipline.alerts_eligibility.broker_freshness",
                   return_value={"current": False}), \
             patch("app.trade_discipline.alerts_eligibility.plan_from_row",
                   side_effect=lambda row: row["plan"]):
            scope = load_alert_scope(connection, account_key="citics-primary", as_of=AS_OF)
        self.assertIn("holding_snapshot_stale_or_unverified", scope.blockers)
        self.assertEqual(scope.plans, ())
        self.assertEqual(scope.excluded[0]["reason"], "recommendation_decision_evidence_mismatch")


class TransitionPersistenceTests(unittest.TestCase):
    def test_only_first_armed_to_triggered_edge_enters_dedicated_outbox(self):
        connection = _TransitionConnection()
        plan = alert_plan()
        with patch("app.trade_discipline.alerts_repository.persist_evaluation",
                   side_effect=lambda _connection, value: {"evaluation_id": value.inputs_hash[:8]}):
            baseline = persist_evaluation_transitions(
                connection, plan_id="plan-1", plan=plan, evaluation=evaluation("armed"))
            first = persist_evaluation_transitions(
                connection, plan_id="plan-1", plan=plan, evaluation=evaluation("triggered"))
            repeated = persist_evaluation_transitions(
                connection, plan_id="plan-1", plan=plan, evaluation=evaluation("triggered"))
        self.assertEqual(baseline["baselines"], 1)
        self.assertEqual(len(first["events"]), 1)
        self.assertEqual(repeated["events"], [])
        self.assertEqual(len(connection.events), 1)
        self.assertEqual(len(connection.deliveries), 1)

    def test_a_triggered_historical_baseline_is_suppressed(self):
        connection = _TransitionConnection()
        with patch("app.trade_discipline.alerts_repository.persist_evaluation",
                   return_value={"evaluation_id": "eval-1"}):
            result = persist_evaluation_transitions(
                connection, plan_id="plan-1", plan=alert_plan(), evaluation=evaluation("triggered"))
        self.assertEqual(result["baselines"], 1)
        self.assertEqual(result["events"], [])
        self.assertTrue(next(iter(connection.states.values()))["baseline_suppressed"])

    def test_settled_daily_line_uses_the_same_durable_transition_path(self):
        connection = _TransitionConnection()
        plan = alert_plan(basis="daily")
        with patch("app.trade_discipline.alerts_repository.persist_evaluation",
                   side_effect=lambda _connection, value: {"evaluation_id": value.inputs_hash[:8]}):
            persist_evaluation_transitions(
                connection, plan_id="plan-daily", plan=plan,
                evaluation=evaluation("armed", basis="daily"))
            result = persist_evaluation_transitions(
                connection, plan_id="plan-daily", plan=plan,
                evaluation=evaluation("triggered", basis="daily"))
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(len(connection.deliveries), 1)


class RuntimeCoverageTests(unittest.TestCase):
    @staticmethod
    def deps(*, active=False):
        async def run_database(call):
            return call()

        return DisciplineAlertRuntimeDependencies(
            database=object(), run_database=run_database, fetch_minutes=AsyncMock(),
            post_text=AsyncMock(return_value={"status": "sent"}),
            session_open=AsyncMock(return_value=(active, "test_session")), dashboard_url=lambda: None,
            account_key=lambda: "citics-primary", now=lambda: AS_OF, interval_seconds=lambda: 30,
        )

    def test_scope_blockers_make_an_active_cycle_blocked(self):
        deps = self.deps(active=True)
        scope = AlertScope(account_key="citics-primary", blockers=("holding_snapshot_stale_or_unverified",))
        with patch("app.trade_discipline.alerts_runtime._load_scope", return_value=scope), \
             patch("app.trade_discipline.alerts_runtime._fetch_tapes", new=AsyncMock(return_value=({}, {}))), \
             patch("app.trade_discipline.alerts_runtime._deliver_due", new=AsyncMock(return_value={"attempted": 0})), \
             patch("app.trade_discipline.alerts_runtime._write_status"):
            result = asyncio.run(run_discipline_alert_cycle(deps, now=AS_OF))
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "authoritative_scope_unavailable")

    def test_weekend_post_close_is_idle_and_never_invokes_daily_evaluation(self):
        deps = self.deps(active=False)
        sunday = datetime(2026, 9, 20, 18, 0, tzinfo=SH)
        with patch("app.trade_discipline.alerts_runtime._calendar_open", return_value=None), \
             patch("app.trade_discipline.alerts_runtime._write_status"), \
             patch("app.trade_discipline.alerts_runtime._deliver_due",
                   new=AsyncMock(return_value={"attempted": 0})), \
             patch("app.trade_discipline.alerts_runtime.run_discipline_alert_daily_cycle",
                   new=AsyncMock()) as daily:
            result = asyncio.run(run_discipline_alert_cycle(deps, now=sunday))
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["reason"], "exchange_calendar_closed")
        daily.assert_not_awaited()

    def test_completed_daily_cycle_is_not_repeated(self):
        deps = self.deps(active=False)
        after_close = datetime(2026, 9, 21, 18, 0, tzinfo=SH)
        with patch("app.trade_discipline.alerts_runtime._calendar_open", return_value=True), \
             patch("app.trade_discipline.alerts_runtime._daily_cycle_completed", return_value=True), \
             patch("app.trade_discipline.alerts_runtime._deliver_due",
                   new=AsyncMock(return_value={"attempted": 1, "sent": 1})) as deliver, \
             patch("app.trade_discipline.alerts_runtime.run_discipline_alert_daily_cycle",
                   new=AsyncMock()) as daily:
            result = asyncio.run(run_discipline_alert_cycle(deps, now=after_close))
        self.assertEqual(result["reason"], "daily_cycle_already_completed")
        self.assertEqual(result["next_delay_seconds"], 300)
        self.assertEqual(result["delivery"]["sent"], 1)
        deliver.assert_awaited_once()
        daily.assert_not_awaited()


class StartupAndSchemaContractTests(unittest.TestCase):
    def test_global_scheduler_off_may_start_only_the_opted_in_leased_discipline_loop(self):
        import app.main as main

        captured = {}

        def start(specs, _runner):
            captured["enabled"] = [spec.label for spec in specs if spec.enabled]
            return {label: object() for label in captured["enabled"]}

        def catalog(**kwargs):
            return (BackgroundTaskSpec("discipline_alerts", kwargs["enabled"]["discipline_alerts"],
                                       kwargs["loops"]["discipline_alerts"]),)

        with patch.object(main, "background_tasks_enabled", return_value=False), \
             patch.object(main, "discipline_alerts_enabled", return_value=True), \
             patch.object(main, "feishu_alert_transport_configured", return_value=True), \
             patch.object(main, "build_background_task_specs", side_effect=catalog), \
             patch.object(main, "validate_runtime_task_specs"), \
             patch.object(main, "apply_background_runtime_profile", side_effect=lambda specs: specs), \
             patch.object(main, "start_leased_background_tasks", side_effect=start):
            tasks = main._start_application_background_tasks()
        self.assertEqual(set(tasks), {"discipline_alerts"})
        self.assertEqual(captured["enabled"], ["discipline_alerts"])

    def test_global_scheduler_off_without_transport_starts_nothing(self):
        import app.main as main

        with patch.object(main, "background_tasks_enabled", return_value=False), \
             patch.object(main, "discipline_alerts_enabled", return_value=True), \
             patch.object(main, "feishu_alert_transport_configured", return_value=False):
            self.assertEqual(main._start_application_background_tasks(), {})

    def test_migration_uses_a_separate_discipline_outbox(self):
        source = (Path(__file__).resolve().parents[1] / "migrations" / "versions" /
                  "20260920_0109_discipline_alerts.py").read_text(encoding="utf-8")
        self.assertIn("discipline_alert_deliveries", source)
        self.assertIn("discipline_alert_line_states", source)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS quant.intraday_signal_events", source)


if __name__ == "__main__":
    unittest.main()
