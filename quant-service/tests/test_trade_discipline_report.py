"""Coverage for app/trade_discipline/report.py (pure rendering, one temp file).

The card is the artefact a human actually reviews, so the properties asserted
here are the ones a reviewer depends on: every line and every quality check is
printed, every price can be recomputed from the printed inputs, the JSON round
trips back into the contract unchanged, and the same plan always renders the
same bytes.

The plan under test is the 神奇制药 600613.SH acceptance fixture from
``test_trade_discipline_core``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.trade_discipline.contracts import DisciplinePlan
from app.trade_discipline.evaluator import EvaluationInputs, evaluate
from app.trade_discipline.generator import CalendarInfo, generate
from app.trade_discipline.quality import CHECK_IDS
from app.trade_discipline.reconcile import reconcile
from app.trade_discipline.report import (
    CHECK_LABEL,
    REPORT_VERSION,
    action_text,
    condition_text,
    derivation_rows,
    line_rows,
    plan_payload,
    quality_rows,
    render_markdown,
    report_paths,
    sizing_rows,
    slug,
    write_report,
)
from test_trade_discipline_core import (
    SHENQI_POSITION,
    UPCOMING,
    bar,
    rally_inputs,
    shenqi_bars,
    shenqi_inputs,
    stage_inputs,
)

SH = ZoneInfo("Asia/Shanghai")
CALENDAR = CalendarInfo(upcoming_trading_dates=UPCOMING, closure_gaps=[])
BREAKDOWN_BAR = bar("2026-09-21", 8.30, 8.35, 7.50, 7.60, 15000)
PLAN_ID = "11111111-2222-3333-4444-555555555555"


def plan_fixture(**overrides):
    return generate(shenqi_inputs(**overrides))


def evaluation_fixture(plan):
    return evaluate(plan, EvaluationInputs(
        plan_id=PLAN_ID, as_of=datetime(2026, 9, 21, 15, 30, tzinfo=SH), basis="daily",
        bars=[*shenqi_bars(), BREAKDOWN_BAR], calendar=CALENDAR, sector_change_pct=-1.2))


def compliance_fixture(plan, evaluation):
    trade = {"trade_record_id": "trade-0001", "trade_date": date(2026, 9, 21), "trade_time": "14:30:00",
             "symbol": "600613.SH", "side": "sell", "quantity": 5800, "price": Decimal("7.62"),
             "name": "神奇制药"}
    return reconcile(plan, [evaluation], [trade], as_of=datetime(2026, 9, 25, 15, 0, tzinfo=SH),
                     calendar=CALENDAR, plan_id=PLAN_ID)


class CardCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.card = render_markdown(self.plan, plan_id=PLAN_ID)

    def test_every_line_appears_with_its_wording_condition_and_action(self):
        self.assertTrue(self.plan.lines)
        for row in line_rows(self.plan):
            self.assertIn(row["label"].replace("|", "\\|"), self.card, row["kind"])
            self.assertIn(row["condition"].replace("|", "\\|"), self.card, row["kind"])
            self.assertIn(row["action"], self.card, row["kind"])
            self.assertIn(row["rule_id"], self.card, row["kind"])

    def test_every_quality_check_appears_with_its_verdict(self):
        for check_id in CHECK_IDS:
            self.assertIn(check_id, self.card)
            self.assertIn(CHECK_LABEL[check_id], self.card)
        self.assertEqual({row["check_id"] for row in quality_rows(self.plan)}, set(CHECK_IDS))
        self.assertIn("全部通过", self.card)

    def test_the_header_states_stage_position_validity_and_status(self):
        for fragment in ("神奇制药 600613.SH", "crash_rebound", "急跌反弹段", "5800 股",
                         self.plan.valid_until.isoformat(), self.plan.inputs_hash, REPORT_VERSION):
            self.assertIn(fragment, self.card)

    def test_the_sizing_table_prints_the_whole_position_formula(self):
        sizing = self.plan.sizing
        for fragment in ("账户权益 equity", "风险预算", "止损距离", "风险上限 max_shares",
                         "建议持仓 recommended_shares", str(sizing.recommended_shares)):
            self.assertIn(fragment, self.card)

    def test_the_derivation_table_recomputes_every_price_it_prints(self):
        rows = derivation_rows(self.plan)
        self.assertEqual(len(rows), len(self.plan.lines))
        priced = [row for row in rows if row["price"] is not None]
        self.assertTrue(priced)
        for row in priced:
            self.assertIsNotNone(row["recomputed"], row["rule_id"])
            self.assertTrue(row["matches"], row["rule_id"])
            self.assertIn(row["formula"], self.card)

    def test_the_card_names_its_research_only_boundary(self):
        self.assertIn("不下单", self.card)
        self.assertFalse(self.plan.quality == [])

    def test_a_rejected_plan_prints_its_failing_checks_instead_of_hiding_them(self):
        broken = self.plan.model_copy(update={
            "status": "rejected_by_quality",
            "quality": [check.model_copy(update={"passed": False, "detail": "人为构造的失败"})
                        if check.check_id == "has_time_stop" else check for check in self.plan.quality]})
        card = render_markdown(broken)
        self.assertIn("未通过 1 项：has_time_stop", card)
        self.assertIn("人为构造的失败", card)
        self.assertIn("未通过质量门（仍已落库）", card)


class WordingTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def test_a_price_line_reads_as_a_sentence_not_as_a_template(self):
        hard = [line for line in self.plan.lines if line.kind == "hard_stop"
                and line.confirm.basis == "daily"][0]
        self.assertEqual(condition_text(hard), f"日线收盘价低于 {hard.price}（日线确认）")
        self.assertEqual(action_text(hard), "全部退出")

    def test_a_multi_bar_minute_line_states_its_confirmation_count(self):
        intraday = [line for line in self.plan.lines if line.kind == "hard_stop"
                    and line.confirm.basis == "minute"][0]
        self.assertIn("连续 3 根分钟确认", condition_text(intraday))

    def test_extra_conditions_are_spelled_out_in_chinese(self):
        soft = generate(rally_inputs()).lines_of("soft_stop")[0]
        self.assertIn("所属行业当日翻绿", condition_text(soft))

    def test_a_time_line_states_its_deadline_rather_than_a_price_comparison(self):
        exposure = self.plan.lines_of("exposure")[0]
        self.assertEqual(condition_text(exposure), "到点执行：下一交易日开盘后15分钟内")
        self.assertIn("减到", action_text(exposure))
        time_stop = self.plan.lines_of("time_stop")[0]
        self.assertIn("个交易日内收盘仍未站回", condition_text(time_stop))


class DisclosureTests(unittest.TestCase):
    """What the card must say about the position and about the lines it refused."""

    def test_the_sizing_table_states_sellable_shares_and_the_t1_lock(self):
        locked = plan_fixture(position={**SHENQI_POSITION, "sellable_quantity": 0})
        self.assertEqual(locked.metrics["t1_locked_shares"], 5800)
        card = render_markdown(locked)
        self.assertIn("| 当日可卖 sellable_quantity | 0 股 |", card)
        self.assertIn("生成日不可卖 5800 股（T+1），价格线自下一交易日起可执行。", card)
        payload = plan_payload(locked)
        self.assertEqual(payload["t1_locked_shares"], 5800)
        self.assertEqual(len(payload["sizing_notes"]), 1)
        # nothing about the lock turns the plan away: it is a disclosure, not a gate
        self.assertEqual(locked.status, "active")

        free = plan_fixture()
        self.assertEqual(free.metrics["t1_locked_shares"], 0)
        self.assertNotIn("生成日不可卖", render_markdown(free))
        self.assertIn("| 当日可卖 sellable_quantity | 5800 股 |", render_markdown(free))

    def test_the_sizing_table_prints_both_exposure_bases_and_the_open_risk(self):
        plan = plan_fixture()
        rows = {row["key"]: row for row in sizing_rows(plan)}
        self.assertEqual(rows["current_risk_pct"]["value"], f"{plan.sizing.current_risk_pct}%")
        self.assertIn("按参考价", rows["current_exposure_pct"]["label"])
        self.assertEqual(rows["current_exposure_pct"]["value"], "48.96%")
        self.assertIn("按快照市值", rows["snapshot_exposure_pct"]["label"])
        self.assertEqual(rows["snapshot_exposure_pct"]["value"], "48.96%")   # 48778 / 99632
        keys = [row["key"] for row in sizing_rows(plan)]
        self.assertEqual(keys.index("current_risk_pct"), keys.index("risk_per_trade_pct") + 1)
        card = render_markdown(plan)
        self.assertIn("current_risk_pct", card)
        self.assertIn("市值 market_value / equity", card)

    def test_the_card_lists_the_lines_it_refused_and_why(self):
        plan = plan_fixture()
        card = render_markdown(plan)
        self.assertIn("未生成的线及原因：", card)
        self.assertIn("| 软止损 |", card)
        self.assertIn("间距不足", card)
        rows = plan_payload(plan)["omitted_lines"]
        self.assertEqual([row["kind"] for row in rows], ["soft_stop"])
        self.assertEqual(rows[0]["inputs"]["hard_stop"], float(plan.sizing.hard_stop))
        broken = generate(stage_inputs("broken"))
        self.assertIn("移动止损", render_markdown(broken).split("未生成的线及原因：")[1].split("## 三")[0])
        complete = generate(rally_inputs())
        self.assertIn("无（模板中的每条可选线都已生成）", render_markdown(complete))

    def test_the_derivation_table_recomputes_the_trail_target_too(self):
        plan = plan_fixture()
        trail = [row for row in derivation_rows(plan) if row["kind"] == "trail"][0]
        self.assertTrue(trail["action_formula"])
        self.assertTrue(trail["action_matches"])
        self.assertIn(trail["action_formula"], render_markdown(plan))
        exposure = [row for row in derivation_rows(plan) if row["kind"] == "exposure"][0]
        self.assertEqual(exposure["action_formula"], "")
        self.assertIsNone(exposure["action_matches"])


class JsonContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def test_the_json_card_round_trips_through_the_contract(self):
        payload = plan_payload(self.plan, plan_id=PLAN_ID)
        restored = DisciplinePlan(**payload["plan"])
        self.assertEqual(restored, self.plan)
        self.assertEqual(restored.model_dump(mode="json"), self.plan.model_dump(mode="json"))

    def test_the_json_card_survives_a_serialization_round_trip(self):
        payload = plan_payload(self.plan, plan_id=PLAN_ID)
        reloaded = json.loads(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        self.assertEqual(DisciplinePlan(**reloaded["plan"]), self.plan)
        self.assertEqual(reloaded["report_version"], REPORT_VERSION)
        self.assertEqual(reloaded["plan_id"], PLAN_ID)
        self.assertTrue(reloaded["quality_passed"])
        self.assertEqual(reloaded["quality"]["failed"], [])
        self.assertEqual(len(reloaded["lines"]), len(self.plan.lines))
        self.assertEqual(reloaded["evidence_refs"], self.plan.evidence_refs)

    def test_a_failing_gate_is_visible_in_the_json_payload(self):
        broken = self.plan.model_copy(update={
            "status": "rejected_by_quality",
            "quality": [check.model_copy(update={"passed": False}) if check.check_id == "sizing_consistent"
                        else check for check in self.plan.quality]})
        payload = plan_payload(broken)
        self.assertFalse(payload["quality_passed"])
        self.assertEqual(payload["quality"]["failed"], ["sizing_consistent"])

    def test_evaluation_and_compliance_are_projected_when_supplied(self):
        evaluation = evaluation_fixture(self.plan)
        records = compliance_fixture(self.plan, evaluation)
        payload = plan_payload(self.plan, evaluation=evaluation, compliance=records, plan_id=PLAN_ID)
        self.assertEqual(payload["evaluation"]["plan_state"], "exit_signalled")
        self.assertIn("已发出退出信号", payload["evaluation"]["plan_state_label"])
        self.assertIn("triggered", {row["state"] for row in payload["evaluation"]["line_states"]})
        self.assertIn("followed", {row["verdict"] for row in payload["compliance"]})
        card = render_markdown(self.plan, evaluation=evaluation, compliance=records, plan_id=PLAN_ID)
        self.assertIn("## 六、最新评估", card)
        self.assertIn("## 七、成交对账", card)
        self.assertIn("遵守", card)
        self.assertIn("trade-0001", card)


class DeterminismAndFileTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def test_rendering_twice_gives_identical_bytes(self):
        first = render_markdown(self.plan, plan_id=PLAN_ID)
        second = render_markdown(generate(shenqi_inputs()), plan_id=PLAN_ID)
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(plan_payload(self.plan), sort_keys=True, ensure_ascii=False),
                         json.dumps(plan_payload(generate(shenqi_inputs())), sort_keys=True, ensure_ascii=False))

    def test_the_file_name_is_windows_safe_and_dated_by_the_trading_day(self):
        paths = report_paths("G:/reports/discipline", self.plan)
        self.assertEqual(paths["directory"].name, "2026-09-18")
        self.assertNotIn(":", paths["markdown"].name)
        self.assertTrue(paths["markdown"].name.startswith("600613.SH-"))
        self.assertEqual(paths["json"].suffix, ".json")
        self.assertEqual(slug("citics-primary:600613.SH:2026-09-18:holding"),
                         "citics-primary-600613.SH-2026-09-18-holding")

    def test_write_report_writes_both_cards_and_reports_where_they_landed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = write_report(self.plan, output_root=directory, plan_id=PLAN_ID)
            markdown = Path(result["markdown_path"])
            payload = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
            self.assertTrue(markdown.exists())
            self.assertEqual(markdown.read_text(encoding="utf-8"),
                             render_markdown(self.plan, plan_id=PLAN_ID))
            self.assertEqual(DisciplinePlan(**payload["plan"]), self.plan)
            self.assertEqual(result["report_version"], REPORT_VERSION)
            # writing the same plan twice must not change a byte
            again = write_report(self.plan, output_root=directory, plan_id=PLAN_ID)
            self.assertEqual(again["markdown_bytes"], result["markdown_bytes"])


class NewBuyCardTests(unittest.TestCase):
    def test_a_new_buy_plan_prints_its_trigger_and_cancel_lines(self):
        plan = generate(stage_inputs("breakout_hold", position=None,
                                     lane={"lane": "contraction", "reference": "10.45", "support": "9.90"}))
        card = render_markdown(plan)
        self.assertEqual(plan.plan_kind, "new_buy")
        self.assertIn("买入触发", card)
        self.assertIn("计划作废", card)
        self.assertIn("成交额不低于前一日", card)
        self.assertIn("新买入计划", card)
        self.assertIn("空仓", card)


if __name__ == "__main__":
    unittest.main()
