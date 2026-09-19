"""New-buy entry price: sized on ``max(lane.reference, latest close)``, never on the structure level.

The 2026-09-18 recommendation pool is the regression fixture.  The three picks'
daily bars are the real settled ``canonical_bars_daily`` rows (exported
read-only into ``fixtures/discipline_real_bars_2026-09-18.json``) and the lane
reference/support levels are the ones the scan published that evening.  The
pre-v3 generator sized all three on the lane reference, several points below
the close, so the stop distance a buyer actually faced was up to 13.9% and the
risk per trade 1.75-2.3% of equity while the gate passed.  Every plan built
here must either keep the risk within 1% and the stop within 12% of the real
entry, or be honestly rejected by the quality gate.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.trade_discipline.evaluator import CAPPED_REASON, EvaluationInputs, evaluate
from app.trade_discipline.generator import (
    GENERATOR_VERSION,
    RESEARCH_CONDITION_NOTE,
    CalendarInfo,
    GenerationInputs,
    generate,
)
from app.trade_discipline.quality import evaluate_quality, failed_checks
from app.trade_discipline.report import plan_payload, render_markdown
from app.trade_discipline.templates import (
    CHASE_CAP_ATR_MULTIPLE,
    STOP_PCT_MAX,
    STOP_PCT_MIN,
    build_sizing,
    hard_stop_price,
)

SH = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 18, 21, 0, tzinfo=SH)
EQUITY = Decimal("98996.26")
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "discipline_real_bars_2026-09-18.json")
                     .read_text(encoding="utf-8"))
REAL_UPCOMING = [date(2026, 9, day) for day in (21, 22, 23, 24, 28, 29, 30)] + [date(2026, 10, 8), date(2026, 10, 9)]
REAL_GAPS = [
    {"last_trading_date": "2026-09-18", "resume_date": "2026-09-21", "closed_days": 2},
    {"last_trading_date": "2026-09-24", "resume_date": "2026-09-28", "closed_days": 3},
    {"last_trading_date": "2026-09-30", "resume_date": "2026-10-08", "closed_days": 7},
]
# (symbol, name, lane, lane reference, lane support, settled close) as published on 2026-09-18
POOL_2026_09_18 = [
    ("603650.SH", "彤程新材", "expansion", "69.31", "63.25", 73.55),
    ("002008.SZ", "大族激光", "accumulation", "95.72", "92.85", 99.60),
    ("000811.SZ", "冰轮环境", "accumulation", "37.94", "36.81", 41.52),
]
RECOMMENDATION = {"decision_id": "7d9e52c8e6d13d6cf83658c36e212bb7e1172b8af623c3488357626c187a1fe1",
                  "as_of_date": "2026-09-18", "priority": 1, "stage": "initial_breakout",
                  "why_now": "放量越过平台", "trigger": "回踩不破平台且放量再上时再考虑",
                  "invalidation": "收盘跌回平台下方"}


def settled(symbol: str) -> list[dict]:
    """``inputs.settled_daily_bars``: the 60-row window with suspended rows dropped."""
    return [row for row in FIXTURE["bars"][symbol] if not row.get("is_suspended")]


def real_inputs(symbol: str, name: str, lane: str, reference: str, support: str, **overrides) -> GenerationInputs:
    payload = {
        "run_id": "run-2026-09-18-pool", "account_key": "citics-primary", "symbol": symbol, "name": name,
        "as_of": AS_OF, "bars": settled(symbol), "equity": EQUITY, "position": None,
        "sector": {"code": "881117", "name": "通用设备", "taxonomy": "longhu_ths_industry"},
        "lane": {"lane": lane, "reference": reference, "support": support, "run_id": "lane-run-0918"},
        "calendar": CalendarInfo(upcoming_trading_dates=REAL_UPCOMING, closure_gaps=REAL_GAPS),
        "evidence_refs": ["bars_basis:settled"],
    }
    payload.update(overrides)
    return GenerationInputs(**payload)


class RealPoolEntryPriceTests(unittest.TestCase):
    """The three 2026-09-18 picks, on their real bars."""

    def test_the_defect_being_fixed_is_real(self):
        """Sizing on the lane reference understated the risk of buying at the close."""
        worst_risk_pct = 0.0
        worst_distance_pct = 0.0
        for symbol, name, lane, reference, support, close in POOL_2026_09_18:
            metrics = generate(real_inputs(symbol, name, lane, reference, support)).metrics
            old_stop, _ = hard_stop_price("breakout_hold", metrics, Decimal(reference))
            old = build_sizing(stage="breakout_hold", equity=EQUITY, risk_per_trade_pct=Decimal("1.0"),
                               reference_price=Decimal(reference), hard_stop=old_stop, current_shares=0)
            real_distance = close - float(old_stop)
            worst_distance_pct = max(worst_distance_pct, real_distance / close * 100)
            worst_risk_pct = max(worst_risk_pct, old.max_shares * real_distance / float(EQUITY) * 100)
        self.assertGreater(worst_distance_pct, 12.0)
        self.assertGreater(worst_risk_pct, 1.5)

    def test_every_pick_is_sized_on_the_close_and_keeps_its_risk_within_one_percent(self):
        for symbol, name, lane, reference, support, close in POOL_2026_09_18:
            with self.subTest(symbol=symbol):
                plan = generate(real_inputs(symbol, name, lane, reference, support))
                self.assertEqual(plan.plan_kind, "new_buy")
                entry = plan.metrics["entry"]
                self.assertEqual(entry["lane_reference"], float(reference))
                self.assertEqual(entry["last_close"], close)
                self.assertEqual(entry["last_close_date"], "2026-09-18")
                self.assertEqual(entry["entry_price"], close)
                self.assertEqual(entry["entry_source"], "last_close")
                self.assertEqual(plan.sizing.reference_price, Decimal(str(close)).quantize(Decimal("0.01")))
                distance = plan.sizing.stop_distance
                distance_pct = float(distance / plan.sizing.reference_price)
                risk_pct = plan.sizing.max_shares * float(distance) / float(EQUITY) * 100
                if plan.status == "active":
                    self.assertLessEqual(risk_pct, 1.0)
                    self.assertLessEqual(plan.sizing.recommended_shares * float(distance) / float(EQUITY) * 100, 1.0)
                    self.assertLessEqual(distance_pct, STOP_PCT_MAX)
                    self.assertGreaterEqual(distance_pct, STOP_PCT_MIN)
                    self.assertEqual(failed_checks(plan.quality), [])
                else:
                    self.assertEqual(plan.status, "rejected_by_quality")
                    self.assertIn("hard_stop_distance_sane", failed_checks(plan.quality))

    def test_the_lane_reference_becomes_the_structure_the_trigger_confirms(self):
        symbol, name, lane, reference, support, close = POOL_2026_09_18[2]
        plan = generate(real_inputs(symbol, name, lane, reference, support))
        trigger = plan.lines_of("trigger")[0]
        cap = plan.lines_of("chase_cap")[0]
        atr14 = plan.metrics["atr14"]
        self.assertEqual(trigger.price, Decimal(reference))
        self.assertEqual(cap.price, Decimal(str(round(close + CHASE_CAP_ATR_MULTIPLE * atr14 + 1e-9, 2))))
        self.assertEqual((cap.metric, cap.op, cap.action.type), ("daily_close", ">", "block_add"))
        self.assertIn("已越过追高上限，不买", cap.label)
        self.assertIn(f"不高于追高上限{cap.price}", trigger.label)
        self.assertLessEqual(abs(cap.derivation.recompute() - float(cap.price)), 0.01)
        self.assertEqual(trigger.derivation.inputs["price_cap"], float(cap.price))
        self.assertEqual(trigger.derivation.inputs["entry_price"], close)
        # trail and take-partial are measured from the entry, not from the structure level
        trail = plan.lines_of("trail")[0]
        self.assertEqual(trail.derivation.action_inputs["anchor_source"], "entry_price")
        self.assertEqual(trail.derivation.action_inputs["anchor_price"], close)
        self.assertEqual(plan.lines_of("take_partial")[0].price, plan.sizing.reference_price)

    def test_the_card_carries_the_pool_wording_as_research_conditions_only(self):
        symbol, name, lane, reference, support, _ = POOL_2026_09_18[0]
        plan = generate(real_inputs(symbol, name, lane, reference, support, recommendation=RECOMMENDATION))
        conditions = plan.metrics["recommendation_conditions"]
        self.assertEqual(conditions["trigger"], RECOMMENDATION["trigger"])
        self.assertEqual(conditions["invalidation"], RECOMMENDATION["invalidation"])
        self.assertEqual(conditions["note"], RESEARCH_CONDITION_NOTE)
        self.assertFalse(conditions["evaluable"])
        payload = plan_payload(plan)
        self.assertEqual([row["key"] for row in payload["recommendation_conditions"]],
                         ["trigger", "invalidation", "why_now"])
        markdown = render_markdown(plan)
        self.assertIn("推荐池研究条件（研究条件，非系统线", markdown)
        self.assertIn("入场参考价", "\n".join(payload["sizing_notes"]))
        # a holding plan never carries the pool wording, even when the symbol is in the pool
        holding = generate(real_inputs(symbol, name, lane, reference, support, recommendation=RECOMMENDATION,
                                       position={"snapshot_id": "s", "observed_at": "2026-09-18T15:10:00+08:00",
                                                 "quantity": 100, "sellable_quantity": 100,
                                                 "average_cost": "70.00"}))
        self.assertEqual(holding.plan_kind, "holding")
        self.assertNotIn("recommendation_conditions", holding.metrics)
        self.assertNotIn("entry", holding.metrics)

    def test_the_generator_version_is_part_of_the_evidence_fingerprint(self):
        symbol, name, lane, reference, support, _ = POOL_2026_09_18[1]
        inputs = real_inputs(symbol, name, lane, reference, support)
        self.assertEqual(inputs.generator_version, GENERATOR_VERSION)
        older = inputs.model_copy(update={"generator_version": "trade-discipline-generator-v2"})
        self.assertNotEqual(inputs.fingerprint(), older.fingerprint())
        self.assertNotEqual(generate(inputs).plan_key, generate(older).plan_key)


class EntryReferenceQualityTests(unittest.TestCase):
    def setUp(self):
        symbol, name, lane, reference, support, _ = POOL_2026_09_18[2]
        self.plan = generate(real_inputs(symbol, name, lane, reference, support))

    def verdict(self, **update):
        changed = self.plan.model_copy(update=update)
        return {check.check_id: check for check in evaluate_quality(changed)}["entry_reference_current"]

    def test_a_current_entry_passes(self):
        check = self.verdict()
        self.assertTrue(check.passed, check.detail)
        self.assertIn("41.52", check.detail)

    def test_a_plan_sized_on_the_structure_level_is_rejected(self):
        metrics = {key: value for key, value in self.plan.metrics.items() if key != "entry"}
        self.assertFalse(self.verdict(metrics=metrics).passed)
        wrong = {**self.plan.metrics, "entry": {**self.plan.metrics["entry"], "entry_price": 37.94}}
        self.assertFalse(self.verdict(metrics=wrong).passed)
        sized_low = self.plan.sizing.model_copy(update={"reference_price": Decimal("37.94")})
        self.assertFalse(self.verdict(sizing=sized_low).passed)

    def test_an_entry_from_an_older_session_is_rejected(self):
        stale_close = {**self.plan.metrics, "entry": {**self.plan.metrics["entry"], "last_close_date": "2026-09-17"}}
        self.assertFalse(self.verdict(metrics=stale_close).passed)
        stale_ref = [*self.plan.evidence_refs, "bars_stale_day:2026-09-21:last_settled:2026-09-18"]
        check = self.verdict(evidence_refs=stale_ref)
        self.assertFalse(check.passed)
        self.assertIn("尚未结算", check.detail)

    def test_a_forming_close_later_than_the_plan_day_is_current(self):
        bars = [*settled("000811.SZ"),
                {"trading_date": "2026-09-21", "open": 41.6, "high": 42.3, "low": 41.2, "close": 42.0,
                 "volume": 1.0e6, "amount": 4.2e7, "forming": True}]
        symbol, name, lane, reference, support, _ = POOL_2026_09_18[2]
        plan = generate(real_inputs(symbol, name, lane, reference, support, bars=bars,
                                    as_of=datetime(2026, 9, 21, 10, 30, tzinfo=SH)))
        self.assertEqual(plan.metrics["entry"]["last_close_basis"], "forming")
        self.assertEqual(plan.metrics["entry"]["entry_price"], 42.0)
        self.assertTrue({check.check_id: check for check in plan.quality}["entry_reference_current"].passed)

    def test_holding_plans_are_not_subject_to_the_check(self):
        holding = self.plan.model_copy(update={"plan_kind": "holding"})
        self.assertTrue({check.check_id: check for check in evaluate_quality(holding)}
                        ["entry_reference_current"].passed)


class ChaseCapEvaluationTests(unittest.TestCase):
    def setUp(self):
        symbol, name, lane, reference, support, _ = POOL_2026_09_18[2]
        self.plan = generate(real_inputs(symbol, name, lane, reference, support))
        self.cap = float(self.plan.lines_of("chase_cap")[0].price)

    def evaluate_close(self, close: float):
        bars = [*settled("000811.SZ"),
                {"trading_date": "2026-09-21", "open": 41.5, "high": max(close, 41.6) + 0.1, "low": 41.0,
                 "close": close, "volume": 3.0e7, "amount": 9.9e9}]
        return evaluate(self.plan, EvaluationInputs(
            plan_id="p", as_of=datetime(2026, 9, 21, 15, 30, tzinfo=SH), bars=bars,
            calendar=CalendarInfo(upcoming_trading_dates=REAL_UPCOMING), sector_change_pct=0.5))

    def states(self, evaluation):
        return {state.kind: state for state in evaluation.line_states}

    def test_a_confirming_close_under_the_cap_is_a_trigger(self):
        states = self.states(self.evaluate_close(round(self.cap - 0.3, 2)))
        self.assertEqual(states["trigger"].state, "triggered")
        self.assertEqual(states["chase_cap"].state, "armed")

    def test_a_confirming_close_above_the_cap_is_capped_not_a_buy(self):
        states = self.states(self.evaluate_close(round(self.cap + 0.5, 2)))
        self.assertEqual(states["trigger"].state, "capped")
        self.assertEqual(states["trigger"].evidence["capped_reason"], CAPPED_REASON)
        self.assertEqual(states["trigger"].evidence["price_cap"], self.cap)
        self.assertEqual(states["chase_cap"].state, "triggered")


if __name__ == "__main__":
    unittest.main()
