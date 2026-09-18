"""Pure coverage for app/trade_discipline/evaluator.py (no database, no HTTP).

The plan under evaluation is the 神奇制药 600613.SH acceptance fixture from
``test_trade_discipline_core``: stage ``crash_rebound``, hard stop 7.75 (daily
plus a 3-minute copy), soft stop 8.38 gated on the sector, take-partial gated on
``after_volume_climax`` + ``below_vwap``, trail arm 9.22, no-add / time-stop
confirmation 8.70 and a next-open exposure cut to 1500 shares.

Every case states the tape explicitly, so a failure names the rule, not a mood.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.trade_discipline.evaluator import (
    EvaluationInputs,
    daily_observations,
    evaluate,
    extra_conditions,
    minute_observations,
)
from app.trade_discipline.generator import CalendarInfo, generate
from test_trade_discipline_core import (
    STAGE_CLOSES,
    UPCOMING,
    bar,
    series_from_closes,
    shenqi_bars,
    shenqi_inputs,
    stage_inputs,
)

SH = ZoneInfo("Asia/Shanghai")
CALENDAR = CalendarInfo(upcoming_trading_dates=UPCOMING, closure_gaps=[])
HOLIDAY_CALENDAR = CalendarInfo(
    upcoming_trading_dates=[date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),
                            date(2026, 10, 9), date(2026, 10, 12)],
    closure_gaps=[{"last_trading_date": "2026-09-30", "resume_date": "2026-10-09", "closed_days": 8}])

# 09-18 is the plan session.  Everything below is tape observed afterwards.
BREAKDOWN_BAR = bar("2026-09-21", 8.30, 8.35, 7.50, 7.60, 15000)
HOLD_BAR = bar("2026-09-21", 8.35, 8.68, 8.28, 8.60, 7000)
RECLAIM_BAR = bar("2026-09-21", 8.30, 8.92, 8.10, 8.85, 9000)
# 20000 万股 beats the whole 20-day history and the close sits in the lower half.
CLIMAX_BAR = bar("2026-09-21", 8.55, 8.60, 7.90, 8.00, 20000)
QUIET_BAR = bar("2026-09-21", 8.40, 8.45, 8.20, 8.30, 3000)


def minutes(*pairs, day="2026-09-21", vwap=8.20, complete=True):
    return [{"session_date": day, "time": stamp, "close": close, "vwap": vwap,
             "volume_lot": 1200, "amount": 1200 * close * 100, "is_complete": complete}
            for stamp, close in pairs]


def stage_bars(stage="breakout_hold"):
    return series_from_closes(STAGE_CLOSES[stage])


def plan_fixture(**overrides):
    return generate(shenqi_inputs(**overrides))


def inputs(**overrides):
    payload = {"plan_id": "plan-600613-2026-09-18", "as_of": datetime(2026, 9, 21, 15, 30, tzinfo=SH),
               "basis": "daily", "bars": shenqi_bars(), "minutes": [], "calendar": CALENDAR,
               "sector_change_pct": -1.2}
    payload.update(overrides)
    return EvaluationInputs(**payload)


def state_of(evaluation, kind, basis=None):
    states = [state for state in evaluation.line_states
              if state.kind == kind and (basis is None or state.basis == basis)]
    if not states:
        raise AssertionError(f"no {kind} line state in the evaluation")
    return states[0]


class ExtraConditionTests(unittest.TestCase):
    """Each of the seven enum conditions, once computable and once not."""

    def test_sector_conditions_need_a_supplied_sector_change(self):
        weak = extra_conditions(shenqi_bars(), [], -1.2)
        strong = extra_conditions(shenqi_bars(), [], 0.4)
        blind = extra_conditions(shenqi_bars(), [], None)
        self.assertTrue(weak["sector_change_negative"])
        self.assertFalse(weak["sector_not_weak"])
        self.assertTrue(strong["sector_not_weak"])
        self.assertFalse(strong["sector_change_negative"])
        self.assertIsNone(blind["sector_change_negative"])
        self.assertIsNone(blind["sector_not_weak"])

    def test_amount_ge_prev_day_compares_the_last_two_sessions(self):
        rising = extra_conditions([*shenqi_bars(), CLIMAX_BAR], [], None)
        falling = extra_conditions([*shenqi_bars(), QUIET_BAR], [], None)
        self.assertTrue(rising["amount_ge_prev_day"])
        self.assertFalse(falling["amount_ge_prev_day"])

    def test_amount_is_not_invented_when_the_bar_has_none(self):
        headless = {**CLIMAX_BAR, "amount": None}
        self.assertIsNone(extra_conditions([*shenqi_bars(), headless], [], None)["amount_ge_prev_day"])

    def test_volume_expansion_and_contraction_use_the_prior_five_session_mean(self):
        loud = extra_conditions([*shenqi_bars(), CLIMAX_BAR], [], None)
        quiet = extra_conditions([*shenqi_bars(), QUIET_BAR], [], None)
        self.assertTrue(loud["volume_expand_1_5x"])
        self.assertFalse(loud["volume_contract_0_7x"])
        self.assertTrue(quiet["volume_contract_0_7x"])
        self.assertFalse(quiet["volume_expand_1_5x"])

    def test_volume_conditions_are_unavailable_without_a_volume_column(self):
        blind = extra_conditions([{**row, "volume": None} for row in shenqi_bars()], [], None)
        self.assertIsNone(blind["volume_expand_1_5x"])
        self.assertIsNone(blind["volume_contract_0_7x"])

    def test_after_volume_climax_needs_the_20_day_high_volume_and_a_weak_close(self):
        climax = extra_conditions([*shenqi_bars(), CLIMAX_BAR], [], None)
        strong_close = extra_conditions([*shenqi_bars(), bar("2026-09-21", 7.95, 8.60, 7.90, 8.55, 20000)], [], None)
        self.assertTrue(climax["after_volume_climax"])
        self.assertFalse(strong_close["after_volume_climax"])   # same volume, close in the upper half

    def test_below_vwap_is_read_from_the_minute_tape_only(self):
        under = extra_conditions(shenqi_bars(), minutes(("0930", 8.00)), None)
        over = extra_conditions(shenqi_bars(), minutes(("0930", 8.40)), None)
        self.assertTrue(under["below_vwap"])
        self.assertFalse(over["below_vwap"])
        self.assertIsNone(extra_conditions(shenqi_bars(), [], None)["below_vwap"])


class DailyBasisTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def test_a_daily_close_under_the_hard_stop_triggers_an_exit(self):
        evaluation = evaluate(self.plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR]))
        hard = state_of(evaluation, "hard_stop", "daily")
        self.assertEqual(hard.state, "triggered")
        self.assertEqual(hard.trigger_price, Decimal("7.6"))
        self.assertEqual(hard.triggered_at, datetime(2026, 9, 21, 15, 0, tzinfo=SH))
        self.assertEqual(hard.evidence["threshold"], 7.75)
        self.assertEqual(evaluation.plan_state, "exit_signalled")
        self.assertEqual(evaluation.trading_date, date(2026, 9, 21))

    def test_a_daily_close_above_the_hard_stop_leaves_it_armed(self):
        evaluation = evaluate(self.plan, inputs(bars=[*shenqi_bars(), HOLD_BAR]))
        hard = state_of(evaluation, "hard_stop", "daily")
        self.assertEqual(hard.state, "armed")
        self.assertIsNone(hard.triggered_at)
        self.assertEqual(hard.evidence["longest_streak"], 0)
        self.assertEqual(hard.evidence["last_observation"]["value"], 8.60)
        self.assertNotEqual(evaluation.plan_state, "exit_signalled")

    def test_the_session_of_the_plan_itself_is_never_re_evaluated(self):
        evaluation = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 18, 16, 0, tzinfo=SH)))
        self.assertEqual(evaluation.plan_state, "active")
        for kind in ("hard_stop", "soft_stop", "no_add", "trail"):
            self.assertEqual(state_of(evaluation, kind, "daily").state, "armed")
        self.assertIn("no closed daily observation", state_of(evaluation, "hard_stop", "daily")
                      .evidence["not_evaluated"])

    def test_an_unfinished_session_is_not_treated_as_a_daily_close(self):
        early = evaluate(self.plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR],
                                           as_of=datetime(2026, 9, 21, 11, 0, tzinfo=SH)))
        self.assertEqual(state_of(early, "hard_stop", "daily").evidence["observations"], 0)

    def test_a_minute_line_is_not_judged_by_a_daily_run(self):
        evaluation = evaluate(self.plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR]))
        minute_copy = state_of(evaluation, "hard_stop", "minute")
        self.assertEqual(minute_copy.state, "armed")
        self.assertIn("this run is daily", minute_copy.evidence["not_evaluated"])

    def test_a_sector_gated_soft_stop_stays_armed_when_the_sector_is_unknown(self):
        blind = evaluate(self.plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR], sector_change_pct=None))
        soft = state_of(blind, "soft_stop", "daily")
        self.assertEqual(soft.state, "armed")
        self.assertIn("sector_change_negative", soft.evidence["not_evaluated"])

    def test_a_sector_that_did_not_fall_leaves_the_soft_stop_unmet(self):
        strong = evaluate(self.plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR], sector_change_pct=1.4))
        soft = state_of(strong, "soft_stop", "daily")
        self.assertEqual(soft.state, "armed")
        self.assertEqual(soft.evidence["extra_unmet"], ["sector_change_negative"])

    def test_the_trail_arms_on_the_latest_close_not_on_an_intraday_spike(self):
        rally = bar("2026-09-21", 8.60, 9.60, 8.55, 9.40, 12000)
        evaluation = evaluate(self.plan, inputs(bars=[*shenqi_bars(), rally], sector_change_pct=0.8))
        trail = state_of(evaluation, "trail", "daily")
        self.assertEqual(trail.state, "triggered")
        self.assertEqual(trail.trigger_price, Decimal("9.4"))
        # moving a stop up is not itself a reduce signal; the exposure cut that
        # came due at 09:45 on the same session is what drives the plan state
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").state, "armed")
        self.assertEqual(evaluation.plan_state, "reduce_signalled")
        self.assertEqual(state_of(evaluation, "exposure").state, "triggered")


class MinuteBasisTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def minute_inputs(self, rows, **overrides):
        payload = {"basis": "minute", "minutes": rows, "as_of": datetime(2026, 9, 21, 10, 5, tzinfo=SH)}
        payload.update(overrides)
        return inputs(**payload)

    def test_three_consecutive_minute_closes_confirm_the_intraday_hard_stop(self):
        rows = minutes(("0930", 7.90), ("0931", 7.80), ("0932", 7.74), ("0933", 7.70), ("0934", 7.72))
        evaluation = evaluate(self.plan, self.minute_inputs(rows))
        minute_stop = state_of(evaluation, "hard_stop", "minute")
        self.assertEqual(minute_stop.state, "triggered")
        self.assertEqual(minute_stop.evidence["confirmed_at"], "09:34")
        self.assertEqual(minute_stop.triggered_at, datetime(2026, 9, 21, 9, 34, tzinfo=SH))
        self.assertEqual(evaluation.plan_state, "exit_signalled")

    def test_two_consecutive_minutes_are_not_enough(self):
        rows = minutes(("0930", 7.90), ("0931", 7.74), ("0932", 7.70), ("0933", 7.80), ("0934", 7.72))
        evaluation = evaluate(self.plan, self.minute_inputs(rows))
        minute_stop = state_of(evaluation, "hard_stop", "minute")
        self.assertEqual(minute_stop.state, "armed")
        self.assertEqual(minute_stop.evidence["longest_streak"], 2)
        self.assertEqual(minute_stop.evidence["confirm_bars"], 3)

    def test_the_in_progress_minute_never_confirms(self):
        rows = minutes(("0930", 7.70), ("0931", 7.68))
        rows.append({**minutes(("0932", 7.66))[0], "is_complete": False})
        evaluation = evaluate(self.plan, self.minute_inputs(rows))
        self.assertEqual(state_of(evaluation, "hard_stop", "minute").evidence["observations"], 2)

    def test_minutes_after_as_of_are_ignored(self):
        rows = minutes(("0930", 7.70), ("0931", 7.68), ("1400", 7.60))
        evaluation = evaluate(self.plan, self.minute_inputs(rows, as_of=datetime(2026, 9, 21, 9, 35, tzinfo=SH)))
        self.assertEqual(state_of(evaluation, "hard_stop", "minute").evidence["observations"], 2)

    def test_take_partial_needs_both_a_volume_climax_and_a_price_under_vwap(self):
        rows = minutes(("1420", 8.05), ("1421", 8.00))
        evaluation = evaluate(self.plan, self.minute_inputs(
            rows, bars=[*shenqi_bars(), CLIMAX_BAR], as_of=datetime(2026, 9, 21, 14, 30, tzinfo=SH)))
        partial = state_of(evaluation, "take_partial", "minute")
        self.assertEqual(partial.state, "triggered")
        self.assertEqual(partial.evidence["extra_values"],
                         {"after_volume_climax": True, "below_vwap": True})
        self.assertEqual(evaluation.plan_state, "reduce_signalled")

    def test_take_partial_stays_armed_without_the_volume_climax(self):
        rows = minutes(("1420", 8.05), ("1421", 8.00))
        evaluation = evaluate(self.plan, self.minute_inputs(
            rows, bars=[*shenqi_bars(), QUIET_BAR], as_of=datetime(2026, 9, 21, 14, 30, tzinfo=SH)))
        partial = state_of(evaluation, "take_partial", "minute")
        self.assertEqual(partial.state, "armed")
        self.assertEqual(partial.evidence["extra_unmet"], ["after_volume_climax"])

    def test_take_partial_stays_armed_when_the_tape_carries_no_vwap(self):
        rows = [{**row, "vwap": None} for row in minutes(("1420", 8.05), ("1421", 8.00))]
        evaluation = evaluate(self.plan, self.minute_inputs(
            rows, bars=[*shenqi_bars(), CLIMAX_BAR], as_of=datetime(2026, 9, 21, 14, 30, tzinfo=SH)))
        partial = state_of(evaluation, "take_partial", "minute")
        self.assertEqual(partial.state, "armed")
        self.assertIn("below_vwap", partial.evidence["not_evaluated"])

    def test_a_daily_line_is_not_judged_by_a_minute_run(self):
        evaluation = evaluate(self.plan, self.minute_inputs(minutes(("0930", 7.00))))
        self.assertIn("this run is minute", state_of(evaluation, "hard_stop", "daily").evidence["not_evaluated"])


class TimeLineTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_fixture()

    def test_the_exposure_cut_is_due_only_from_the_next_open_plus_fifteen_minutes(self):
        before = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 21, 9, 44, tzinfo=SH)))
        after = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 21, 9, 45, tzinfo=SH)))
        self.assertEqual(state_of(before, "exposure").state, "armed")
        self.assertFalse(state_of(before, "exposure").evidence["due"])
        self.assertEqual(state_of(after, "exposure").state, "triggered")
        self.assertTrue(state_of(after, "exposure").evidence["due"])
        self.assertEqual(state_of(after, "exposure").basis, "time")
        self.assertIsNone(state_of(after, "exposure").trigger_price)
        self.assertEqual(after.plan_state, "reduce_signalled")

    def test_a_time_line_without_a_calendar_reports_unknown_rather_than_due(self):
        blind = evaluate(self.plan, inputs(calendar=CalendarInfo()))
        self.assertEqual(state_of(blind, "exposure").state, "armed")
        self.assertIsNone(state_of(blind, "exposure").evidence["due"])
        self.assertIsNone(state_of(blind, "time_stop").evidence["due"])

    def test_the_holiday_line_is_due_before_the_close_of_the_last_session(self):
        plan = plan_fixture(calendar=HOLIDAY_CALENDAR)
        self.assertEqual(plan.lines_of("holiday")[0].execute_at, "2026-09-30_before_close")
        args = {"bars": shenqi_bars(), "calendar": HOLIDAY_CALENDAR}
        early = evaluate(plan, inputs(as_of=datetime(2026, 9, 30, 13, 0, tzinfo=SH), **args))
        due = evaluate(plan, inputs(as_of=datetime(2026, 9, 30, 14, 55, tzinfo=SH), **args))
        self.assertEqual(state_of(early, "holiday").state, "armed")
        self.assertEqual(state_of(due, "holiday").state, "triggered")
        # crash_rebound goes flat over a long closure, so the holiday cut is an exit
        self.assertEqual(state_of(due, "holiday").evidence["execute_at"], "2026-09-30_before_close")
        self.assertEqual(due.plan_state, "exit_signalled")

    def test_the_time_stop_counts_trading_days_and_falls_due_at_the_third_close(self):
        bars = [*shenqi_bars(), bar("2026-09-21", 8.30, 8.40, 8.10, 8.20, 9000),
                bar("2026-09-22", 8.20, 8.30, 8.00, 8.10, 9000),
                bar("2026-09-23", 8.10, 8.20, 7.90, 8.00, 9000)]
        args = {"bars": bars, "sector_change_pct": 0.5}
        pending = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 23, 14, 59, tzinfo=SH), **args))
        due = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 23, 15, 0, tzinfo=SH), **args))
        self.assertEqual(state_of(pending, "time_stop").state, "armed")
        self.assertEqual(state_of(pending, "time_stop").evidence["trading_days_remaining"], 0)
        self.assertEqual(state_of(due, "time_stop").state, "triggered")
        self.assertEqual(state_of(due, "time_stop").evidence["rule"], "T+3_close")
        self.assertEqual(state_of(due, "time_stop").triggered_at, datetime(2026, 9, 23, 15, 0, tzinfo=SH))
        self.assertEqual(due.plan_state, "exit_signalled")

    def test_two_sessions_in_is_not_yet_the_time_stop(self):
        bars = [*shenqi_bars(), bar("2026-09-21", 8.30, 8.40, 8.10, 8.20, 9000),
                bar("2026-09-22", 8.20, 8.30, 8.00, 8.10, 9000)]
        evaluation = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 22, 15, 0, tzinfo=SH),
                                                bars=bars, sector_change_pct=0.5))
        self.assertEqual(state_of(evaluation, "time_stop").state, "armed")
        self.assertEqual(state_of(evaluation, "time_stop").evidence["trading_days_remaining"], 1)

    def test_reclaiming_the_confirmation_line_cancels_the_time_stop(self):
        evaluation = evaluate(self.plan, inputs(as_of=datetime(2026, 9, 23, 15, 0, tzinfo=SH),
                                                bars=[*shenqi_bars(), RECLAIM_BAR], sector_change_pct=0.5))
        time_stop = state_of(evaluation, "time_stop")
        self.assertEqual(time_stop.state, "cancelled")
        self.assertEqual(time_stop.evidence["reclaimed_on"], "2026-09-21")
        self.assertEqual(time_stop.evidence["reclaim_close"], 8.85)
        self.assertFalse(time_stop.evidence["due"])


class PlanStateTests(unittest.TestCase):
    def test_an_untriggered_plan_past_its_validity_expires(self):
        # trend_hold sits inside its exposure cap, so no time line falls due and
        # the 09-21 reclaim of the confirmation level cancels the time stop.
        plan = generate(stage_inputs("trend_hold"))
        bars = [*stage_bars("trend_hold"), bar("2026-09-21", 12.95, 13.15, 12.90, 13.10, 3000)]
        evaluation = evaluate(plan, inputs(as_of=datetime(2026, 9, 28, 15, 0, tzinfo=SH),
                                           bars=bars, sector_change_pct=0.5))
        self.assertEqual(evaluation.plan_state, "expired")
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").state, "expired")
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").evidence["valid_until"],
                         plan.valid_until.isoformat())
        self.assertEqual(state_of(evaluation, "time_stop").state, "cancelled")

    def test_a_triggered_exit_outranks_expiry(self):
        plan = plan_fixture()
        evaluation = evaluate(plan, inputs(as_of=datetime(2026, 9, 28, 15, 0, tzinfo=SH),
                                           bars=[*shenqi_bars(), BREAKDOWN_BAR]))
        self.assertEqual(evaluation.plan_state, "exit_signalled")
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").state, "triggered")

    def test_a_triggered_cancel_line_voids_the_rest_of_a_new_buy_plan(self):
        plan = generate(stage_inputs("breakout_hold", position=None,
                                     lane={"lane": "contraction", "reference": "10.45", "support": "9.90"}))
        self.assertEqual(plan.plan_kind, "new_buy")
        breakdown = bar("2026-09-21", 10.00, 10.05, 9.40, 9.50, 40000)
        evaluation = evaluate(plan, inputs(plan_id="plan-new-buy", bars=[*stage_bars(), breakdown],
                                           sector_change_pct=0.5))
        self.assertEqual(state_of(evaluation, "cancel").state, "triggered")
        self.assertEqual(state_of(evaluation, "trigger").state, "cancelled")
        self.assertEqual(state_of(evaluation, "trigger").evidence["voided_by"], "cancel")
        # the entry never happened, so a void plan outranks the stop that the
        # same session would otherwise have fired
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").state, "triggered")
        self.assertEqual(evaluation.plan_state, "expired")

    def test_a_plan_that_never_passed_the_gate_is_not_evaluated_at_all(self):
        plan = plan_fixture().model_copy(update={"status": "rejected_by_quality"})
        evaluation = evaluate(plan, inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR]))
        self.assertEqual(evaluation.plan_state, "expired")
        self.assertEqual({state.state for state in evaluation.line_states}, {"cancelled"})
        self.assertEqual(state_of(evaluation, "hard_stop", "daily").evidence["plan_status"],
                         "rejected_by_quality")


class ObservationTests(unittest.TestCase):
    def test_daily_observations_stop_at_the_plan_session_and_at_as_of(self):
        plan = plan_fixture()
        bars = [*shenqi_bars(), bar("2026-09-21", 8.30, 8.40, 8.10, 8.20, 9000),
                bar("2026-09-22", 8.20, 8.30, 8.00, 8.10, 9000)]
        seen = daily_observations(plan, bars, datetime(2026, 9, 21, 15, 30, tzinfo=SH), "daily_close")
        self.assertEqual([observation.label for observation in seen], ["2026-09-21"])

    def test_minute_observations_are_ordered_and_typed(self):
        rows = minutes(("0932", 8.10), ("0930", 8.20))
        seen = minute_observations(rows, datetime(2026, 9, 21, 15, 0, tzinfo=SH), "minute_close")
        self.assertEqual([observation.label for observation in seen], ["09:30", "09:32"])
        self.assertEqual(seen[0].basis, "minute")

    def test_an_unsupported_metric_yields_no_observations(self):
        plan = plan_fixture()
        self.assertEqual(daily_observations(plan, shenqi_bars(),
                                            datetime(2026, 9, 21, 15, 0, tzinfo=SH), "vwap"), [])


class IdempotenceTests(unittest.TestCase):
    def test_the_same_inputs_produce_an_identical_evaluation(self):
        plan = plan_fixture()
        payload = inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR],
                         minutes=minutes(("0930", 7.70), ("0931", 7.68), ("0932", 7.66)))
        first = evaluate(plan, payload)
        second = evaluate(plan, EvaluationInputs(**payload.model_dump()))
        self.assertEqual(first.inputs_hash, second.inputs_hash)
        self.assertEqual(first.model_dump(mode="json"), second.model_dump(mode="json"))

    def test_a_different_plan_gives_a_different_evaluation_hash(self):
        payload = inputs(bars=[*shenqi_bars(), BREAKDOWN_BAR])
        other = generate(stage_inputs("trend_hold"))
        self.assertNotEqual(evaluate(plan_fixture(), payload).inputs_hash, evaluate(other, payload).inputs_hash)

    def test_evaluation_time_is_recorded_as_supplied(self):
        evaluation = evaluate(plan_fixture(), inputs())
        self.assertEqual(evaluation.as_of_at, datetime(2026, 9, 21, 15, 30, tzinfo=SH))
        self.assertEqual(evaluation.plan_id, "plan-600613-2026-09-18")
        self.assertEqual(len(evaluation.inputs_hash), 64)


if __name__ == "__main__":
    unittest.main()
