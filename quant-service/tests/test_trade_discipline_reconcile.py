"""Pure coverage for app/trade_discipline/reconcile.py (no database, no HTTP).

The plan is the 神奇制药 600613.SH acceptance fixture, the evaluations come from
the real evaluator, and the fills use the ``quant.broker_trade_records`` shape.
Every one of the six contract verdicts has a worked example here, and each
example states the deviation a review would read: price, time and quantity.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.trade_discipline.evaluator import evaluate
from app.trade_discipline.generator import generate
from app.trade_discipline.reconcile import (
    VERDICTS,
    TradeRecord,
    no_add_release,
    reconcile,
    signals_from,
)
from test_trade_discipline_evaluator import (
    BREAKDOWN_BAR,
    CALENDAR,
    RECLAIM_BAR,
    inputs,
    plan_fixture,
    stage_bars,
)
from test_trade_discipline_core import bar, rally_bars, rally_inputs, shenqi_bars, stage_inputs

SH = ZoneInfo("Asia/Shanghai")
PLAN_ID = "plan-600613-2026-09-18"
VALID_UNTIL = datetime(2026, 9, 25, 15, 0, tzinfo=SH)
BREAKDOWN_BARS = [*shenqi_bars(), BREAKDOWN_BAR]


def trade(record_id, day, stamp, side, quantity, price, symbol="600613.SH"):
    return {"trade_record_id": record_id, "trade_date": date.fromisoformat(day),
            "trade_time": time.fromisoformat(stamp), "symbol": symbol, "side": side,
            "quantity": quantity, "price": Decimal(str(price)), "name": "神奇制药"}


def breakdown_evaluations(plan):
    """09-21 closes at 7.60; a plan with an actual risk overflow also fires exposure."""
    return [evaluate(plan, inputs(bars=BREAKDOWN_BARS, as_of=datetime(2026, 9, 21, 15, 30, tzinfo=SH)))]


def quiet_evaluations(plan):
    """The evening of the plan session itself: nothing has been observed yet."""
    return [evaluate(plan, inputs(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=SH)))]


def by_kind(records, verdict=None):
    return {(record.line_kind, record.verdict): record for record in records
            if verdict is None or record.verdict == verdict}


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture(risk_per_trade_pct=Decimal("1.0"))
        self.signals = signals_from(self.plan, breakdown_evaluations(self.plan))

    def test_triggered_lines_become_expected_fills(self):
        # no soft stop on this fixture: MA5 sits within half an ATR of the close and the line is refused
        self.assertEqual({signal.line_kind for signal in self.signals},
                         {"exposure", "hard_stop", "no_add"})
        expected = {signal.line_kind: (signal.side, signal.expected_quantity) for signal in self.signals}
        self.assertEqual(expected["hard_stop"], ("sell", 5800))       # exit_all on the whole position
        # The explicit 1% override makes the stop-risk limit 1500 shares, so
        # the expected fill is the 4300-share reduction from 5800.
        self.assertEqual(expected["exposure"], ("sell", 4300))
        self.assertEqual(expected["no_add"], (None, None))            # a prohibition expects no fill

    def test_a_drawn_soft_stop_expects_a_half_position_fill(self):
        plan = generate(rally_inputs())
        evaluations = [evaluate(plan, inputs(bars=[*rally_bars(), BREAKDOWN_BAR],
                                             as_of=datetime(2026, 9, 21, 15, 30, tzinfo=SH)))]
        expected = {signal.line_kind: (signal.side, signal.expected_quantity)
                    for signal in signals_from(plan, evaluations)}
        self.assertEqual(expected["soft_stop"], ("sell", 2900))       # reduce_by_pct 50, whole lots

    def test_the_daily_hard_stop_and_its_minute_copy_collapse_into_one_expectation(self):
        hard = [signal for signal in self.signals if signal.line_kind == "hard_stop"]
        self.assertEqual(len(hard), 1)
        self.assertEqual(hard[0].line_price, Decimal("7.75"))
        self.assertEqual(hard[0].trigger_price, Decimal("7.6"))

    def test_signals_are_ordered_by_the_moment_they_triggered(self):
        stamps = [signal.triggered_at for signal in self.signals]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(self.signals[0].line_kind, "exposure")   # due at 09:45, before the close

    def test_an_evaluation_with_nothing_triggered_produces_no_signal(self):
        self.assertEqual(signals_from(self.plan, quiet_evaluations(self.plan)), [])


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.triggered = breakdown_evaluations(self.plan)
        self.quiet = quiet_evaluations(self.plan)

    def run_reconcile(self, evaluations, trades, as_of=VALID_UNTIL):
        return reconcile(self.plan, evaluations, trades, as_of=as_of, calendar=CALENDAR, plan_id=PLAN_ID)

    def test_followed_is_a_same_direction_fill_inside_one_trading_day(self):
        records = self.run_reconcile(self.triggered,
                                     [trade("t-1", "2026-09-21", "14:50:00", "sell", 5800, "7.62")])
        self.assertEqual({record.verdict for record in records}, {"followed"})
        stop = by_kind(records)[("hard_stop", "followed")]
        self.assertEqual(stop.plan_id, PLAN_ID)
        self.assertEqual(stop.trade_record_id, "t-1")
        self.assertEqual(stop.deviation["price_expected"], 7.75)
        self.assertEqual(stop.deviation["price_actual"], 7.62)
        self.assertEqual(stop.deviation["price_diff"], -0.13)
        self.assertEqual(stop.deviation["trading_days_diff"], 0)
        self.assertEqual(stop.deviation["trading_days_basis"], "exchange_calendar")
        self.assertEqual(stop.deviation["quantity_expected"], 5800)
        self.assertEqual(stop.deviation["quantity_actual"], 5800)
        self.assertEqual(stop.deviation["quantity_diff"], 0)
        self.assertEqual(stop.deviation["time_expected"], "2026-09-21T15:00:00+08:00")
        self.assertEqual(stop.deviation["time_actual"], "2026-09-21T14:50:00+08:00")

    def test_a_fill_on_the_next_session_is_still_followed(self):
        records = self.run_reconcile(self.triggered,
                                     [trade("t-2", "2026-09-22", "09:35:00", "sell", 4300, "7.40")])
        stop = by_kind(records)[("hard_stop", "followed")]
        self.assertEqual(stop.deviation["trading_days_diff"], 1)
        self.assertEqual(stop.deviation["quantity_diff"], 4300 - 5800)

    def test_late_is_the_same_fill_more_than_one_trading_day_after_the_trigger(self):
        records = self.run_reconcile(self.triggered,
                                     [trade("t-3", "2026-09-24", "10:05:00", "sell", 5800, "7.10")])
        self.assertEqual({record.verdict for record in records}, {"late"})
        stop = by_kind(records)[("hard_stop", "late")]
        self.assertEqual(stop.deviation["trading_days_diff"], 3)
        self.assertEqual(stop.deviation["price_diff"], -0.65)
        self.assertIn("交易日后成交", stop.notes)

    def test_missed_is_a_trigger_that_nothing_answered_before_the_validity_ended(self):
        records = self.run_reconcile(self.triggered, [])
        self.assertEqual({record.verdict for record in records}, {"missed"})
        stop = by_kind(records)[("hard_stop", "missed")]
        self.assertIsNone(stop.trade_record_id)
        self.assertIsNone(stop.deviation["price_actual"])
        self.assertEqual(stop.deviation["quantity_actual"], 0)
        self.assertEqual(stop.deviation["quantity_diff"], -5800)
        self.assertIsNone(stop.deviation["trading_days_diff"])

    def test_a_trigger_still_inside_its_window_is_not_yet_missed(self):
        records = self.run_reconcile(self.triggered, [], as_of=datetime(2026, 9, 22, 15, 0, tzinfo=SH))
        self.assertEqual(records, [])

    def test_a_two_tranche_exit_after_one_trigger_is_obedience_all_the_way_through(self):
        """5800 shares sold in two tranches answer one hard stop, not one plus a violation."""
        records = self.run_reconcile(self.triggered, [
            trade("t-9a", "2026-09-21", "14:40:00", "sell", 3000, "7.62"),
            trade("t-9b", "2026-09-21", "14:55:00", "sell", 2800, "7.58"),
        ])
        hard = [record for record in records if record.line_kind == "hard_stop"]
        self.assertEqual([record.verdict for record in hard], ["followed", "followed"])
        self.assertEqual([record.trade_record_id for record in hard], ["t-9a", "t-9b"])
        self.assertEqual([record.deviation["tranche"] for record in hard], [1, 2])
        self.assertEqual([record.deviation["quantity_filled_cumulative"] for record in hard], [3000, 5800])
        self.assertEqual([record.deviation["quantity_diff"] for record in hard], [-2800, 0])
        self.assertNotIn("early", {record.verdict for record in records})
        for record in records:
            self.assertNotIn("计划内无任何线触发", record.notes)

    def test_a_sell_beyond_what_the_triggered_lines_asked_for_says_so(self):
        """Past the expected quantity the verdict is still ``early`` - with an honest note."""
        records = self.run_reconcile(self.triggered, [
            trade("t-10a", "2026-09-21", "14:40:00", "sell", 5800, "7.62"),
            trade("t-10b", "2026-09-22", "09:40:00", "sell", 200, "7.40"),
        ])
        extra = [record for record in records if record.trade_record_id == "t-10b"]
        self.assertEqual([record.verdict for record in extra], ["early"])
        self.assertIn("已成交完毕", extra[0].notes)
        self.assertIn("hard_stop", extra[0].deviation["triggered_lines"])
        self.assertNotIn("计划内无任何线触发", extra[0].notes)

    def test_a_sell_before_any_trigger_names_the_first_signal_instead_of_denying_it(self):
        records = self.run_reconcile(self.triggered, [
            trade("t-11", "2026-09-18", "14:20:00", "sell", 1000, "8.45"),
            trade("t-12", "2026-09-21", "14:50:00", "sell", 5800, "7.62"),
        ])
        early = [record for record in records if record.trade_record_id == "t-11"]
        self.assertEqual([record.verdict for record in early], ["early"])
        self.assertIn("2026-09-21", early[0].notes)
        self.assertIn("早于任何触发", early[0].notes)

    def test_early_is_a_sell_with_no_line_behind_it(self):
        records = self.run_reconcile(self.quiet,
                                     [trade("t-4", "2026-09-22", "10:20:00", "sell", 2000, "8.30")])
        self.assertEqual([record.verdict for record in records], ["early"])
        early = records[0]
        self.assertIsNone(early.line_kind)
        self.assertIsNone(early.deviation["price_expected"])
        self.assertEqual(early.deviation["price_actual"], 8.30)
        self.assertEqual(early.deviation["quantity_actual"], 2000)
        self.assertEqual(early.deviation["distance_to_lines"]["hard_stop:7.75"], 0.55)
        self.assertFalse([key for key in early.deviation["distance_to_lines"] if key.startswith("soft_stop")])

    def test_against_plan_is_a_buy_while_the_no_add_block_is_in_force(self):
        records = self.run_reconcile(self.triggered,
                                     [trade("t-5", "2026-09-22", "13:10:00", "buy", 1000, "7.80")],
                                     as_of=datetime(2026, 9, 22, 15, 0, tzinfo=SH))
        self.assertEqual([record.verdict for record in records], ["against_plan"])
        against = records[0]
        self.assertEqual(against.line_kind, "no_add")
        self.assertIsNone(against.deviation["block_released_on"])
        self.assertEqual(against.deviation["quantity_actual"], 1000)
        self.assertIn("禁止加仓", against.notes)

    def test_a_buy_after_the_block_lifts_is_unplanned_rather_than_against_plan(self):
        lifted = [*self.triggered,
                  evaluate(self.plan, inputs(bars=[*BREAKDOWN_BARS, bar("2026-09-22", 7.70, 8.95, 7.65, 8.90, 14000)],
                                             as_of=datetime(2026, 9, 22, 15, 30, tzinfo=SH)))]
        self.assertEqual(no_add_release(self.plan, lifted), date(2026, 9, 22))
        records = reconcile(self.plan, lifted,
                            [trade("t-6", "2026-09-23", "09:40:00", "buy", 1000, "9.10")],
                            as_of=datetime(2026, 9, 23, 15, 0, tzinfo=SH), calendar=CALENDAR, plan_id=PLAN_ID)
        self.assertEqual([record.verdict for record in records], ["unplanned"])
        self.assertIsNone(records[0].line_kind)

    def test_unplanned_covers_another_symbol_and_a_fill_outside_the_window(self):
        records = self.run_reconcile(self.quiet, [
            trade("t-7", "2026-09-22", "10:00:00", "sell", 500, "8.30", symbol="600000.SH"),
            trade("t-8", "2026-10-09", "10:00:00", "sell", 500, "8.30"),
        ])
        self.assertEqual({record.verdict for record in records}, {"unplanned"})
        notes = {record.trade_record_id: record.notes for record in records}
        self.assertIn("600000.SH", notes["t-7"])
        self.assertIn("有效期", notes["t-8"])

    def test_every_contract_verdict_has_a_worked_example(self):
        seen: set[str] = set()
        seen.update(record.verdict for record in self.run_reconcile(
            self.triggered, [trade("a", "2026-09-21", "14:50:00", "sell", 5800, "7.62")]))
        seen.update(record.verdict for record in self.run_reconcile(
            self.triggered, [trade("b", "2026-09-24", "10:05:00", "sell", 5800, "7.10")]))
        seen.update(record.verdict for record in self.run_reconcile(self.triggered, []))
        seen.update(record.verdict for record in self.run_reconcile(
            self.quiet, [trade("c", "2026-09-22", "10:20:00", "sell", 2000, "8.30")]))
        seen.update(record.verdict for record in self.run_reconcile(
            self.triggered, [trade("d", "2026-09-22", "13:10:00", "buy", 1000, "7.80")],
            as_of=datetime(2026, 9, 22, 15, 0, tzinfo=SH)))
        seen.update(record.verdict for record in self.run_reconcile(
            self.quiet, [trade("e", "2026-10-09", "10:00:00", "sell", 500, "8.30")]))
        self.assertEqual(seen, set(VERDICTS))


class NewBuyReconcileTests(unittest.TestCase):
    def setUp(self):
        self.plan = generate(stage_inputs("breakout_hold", position=None,
                                          lane={"lane": "contraction", "reference": "10.45", "support": "9.90"}))
        # closes 11.15: above the 10.45 structure and under the 11.22 chase cap (entry 11.00 + 0.5 x ATR14)
        entry = bar("2026-09-21", 11.00, 11.30, 10.90, 11.15, 8000)
        self.evaluations = [evaluate(self.plan, inputs(plan_id="plan-new-buy", bars=[*stage_bars(), entry],
                                                       sector_change_pct=0.6))]

    def test_a_buy_after_the_trigger_fires_is_followed(self):
        self.assertEqual(self.plan.plan_kind, "new_buy")
        records = reconcile(self.plan, self.evaluations,
                            [trade("n-1", "2026-09-22", "09:40:00", "buy",
                                   self.plan.sizing.recommended_shares, "11.50", symbol="600000.SH")],
                            as_of=VALID_UNTIL, calendar=CALENDAR, plan_id="plan-new-buy")
        entry_record = by_kind(records)[("trigger", "followed")]
        self.assertEqual(entry_record.deviation["quantity_expected"], self.plan.sizing.recommended_shares)
        self.assertEqual(entry_record.deviation["quantity_diff"], 0)
        self.assertEqual(entry_record.deviation["trading_days_diff"], 1)
        self.assertEqual(entry_record.deviation["price_expected"], 10.46)   # the trigger floor

    def test_a_new_buy_plan_never_reports_a_buy_as_against_plan(self):
        records = reconcile(self.plan, self.evaluations,
                            [trade("n-2", "2026-09-22", "09:40:00", "buy", 100, "11.50", symbol="600000.SH")],
                            as_of=VALID_UNTIL, calendar=CALENDAR, plan_id="plan-new-buy")
        self.assertNotIn("against_plan", {record.verdict for record in records})
        self.assertIsNone(no_add_release(self.plan, self.evaluations))


class DeterminismTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()
        self.evaluations = breakdown_evaluations(self.plan)
        self.trades = [trade("t-1", "2026-09-24", "10:05:00", "sell", 3000, "7.10"),
                       trade("t-2", "2026-09-22", "13:10:00", "buy", 1000, "7.80"),
                       trade("t-3", "2026-09-22", "10:00:00", "sell", 500, "7.90", symbol="600000.SH")]

    def test_the_same_inputs_produce_identical_records(self):
        first = reconcile(self.plan, self.evaluations, self.trades, as_of=VALID_UNTIL,
                          calendar=CALENDAR, plan_id=PLAN_ID)
        second = reconcile(self.plan, self.evaluations, [TradeRecord(**row) for row in reversed(self.trades)],
                           as_of=VALID_UNTIL, calendar=CALENDAR, plan_id=PLAN_ID)
        self.assertEqual([record.model_dump(mode="json") for record in first],
                         [record.model_dump(mode="json") for record in second])

    def test_records_are_ordered_by_fill_time(self):
        records = reconcile(self.plan, self.evaluations, self.trades, as_of=VALID_UNTIL,
                            calendar=CALENDAR, plan_id=PLAN_ID)
        stamps = [record.deviation.get("time_actual") or "9999" for record in records]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual({record.verdict for record in records}, {"unplanned", "against_plan", "late"})

    def test_a_naive_as_of_is_refused(self):
        with self.assertRaises(ValueError):
            reconcile(self.plan, self.evaluations, [], as_of=datetime(2026, 9, 25, 15, 0))

    def test_the_plan_key_is_used_when_no_plan_id_is_supplied(self):
        records = reconcile(self.plan, [], [trade("t-9", "2026-09-22", "10:20:00", "sell", 100, "8.30")],
                            as_of=VALID_UNTIL, calendar=CALENDAR)
        self.assertEqual(records[0].plan_id, self.plan.plan_key)


class TradeRecordTests(unittest.TestCase):
    def test_a_string_trade_time_is_parsed_and_a_missing_one_falls_back_to_the_close(self):
        parsed = TradeRecord(trade_record_id="x", trade_date=date(2026, 9, 21), trade_time="10:05:00",
                             symbol="600613.SH", side="sell", quantity=100, price=Decimal("8.00"))
        blank = parsed.model_copy(update={"trade_time": None})
        self.assertEqual(parsed.filled_at, datetime(2026, 9, 21, 10, 5, tzinfo=SH))
        self.assertEqual(blank.filled_at, datetime(2026, 9, 21, 15, 0, tzinfo=SH))

    def test_a_zero_quantity_fill_is_refused(self):
        with self.assertRaises(ValueError):
            TradeRecord(trade_record_id="x", trade_date=date(2026, 9, 21), symbol="600613.SH",
                        side="sell", quantity=0, price=Decimal("8.00"))


class ReclaimTests(unittest.TestCase):
    def test_a_reclaim_session_lifts_the_block_and_cancels_the_time_stop(self):
        plan = plan_fixture()
        evaluations = [evaluate(plan, inputs(bars=[*shenqi_bars(), RECLAIM_BAR],
                                             as_of=datetime(2026, 9, 21, 15, 30, tzinfo=SH),
                                             sector_change_pct=0.5))]
        self.assertEqual(no_add_release(plan, evaluations), date(2026, 9, 21))
        records = reconcile(plan, evaluations, [trade("r-1", "2026-09-22", "09:40:00", "buy", 500, "8.95")],
                            as_of=datetime(2026, 9, 22, 15, 0, tzinfo=SH), calendar=CALENDAR, plan_id=PLAN_ID)
        self.assertEqual([record.verdict for record in records], ["unplanned"])


if __name__ == "__main__":
    unittest.main()
