"""Pure-function coverage for app/trade_discipline (no database, no HTTP).

The acceptance fixture is 神奇制药 600613.SH as of 2026-09-18: the 09-09..09-18
daily bars are the real ones, the 08-13..09-08 run-up and give-back is
reconstructed around the real anchors (5.30 -> 12.40, 11.16, 10.04, 9.09) and
every reconstructed bar is flagged ``synthetic`` so the plan's evidence refs say
so.  It must classify as ``crash_rebound`` and pass the whole quality gate.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.trade_discipline.contracts import Action, Confirm, Derivation, Line, PositionRef, QualityCheck
from app.trade_discipline.generator import CalendarInfo, GenerationInputs, generate, t1_locked_shares_for
from app.trade_discipline.quality import CHECK_IDS, evaluate_quality, failed_checks, quality_passed
from app.trade_discipline.stage import classify_stage, daily_metrics, normalize_bars
from app.short_term_lanes.risk import volatility_buffer_pct
from app.short_term_lanes.rules import features as rules_features
from app.trade_discipline.templates import (
    CRASH_FALLBACK_LABEL,
    HARD_STOP_TERMS,
    HOLIDAY_CLOSURE_DAYS,
    LOT_SIZE,
    buffer_pct,
    build_sizing,
    closures_within,
    hard_stop_price,
    is_ordinary_weekend,
    lot_shares,
    _anchor_price,
    soft_stop_window,
    stop_beyond_band,
    trail_stop_price,
)

SH = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 18, 15, 30, tzinfo=SH)
UPCOMING = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)]
NATIONAL_DAY_GAP = {"last_trading_date": "2026-09-30", "resume_date": "2026-10-09", "closed_days": 8}
# A long closure that starts inside the fixture's validity window (09-18..09-25).
IN_WINDOW_GAP = {"last_trading_date": "2026-09-22", "resume_date": "2026-09-28", "closed_days": 5}
WEEKEND_GAP = {"last_trading_date": "2026-09-18", "resume_date": "2026-09-21", "closed_days": 2}


def bar(day, open_, high, low, close, volume_wan, *, synthetic=False):
    volume = float(volume_wan) * 10_000
    return {"trading_date": day, "open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "amount": round(volume * (high + low + 2 * close) / 4, 2),
            "synthetic": synthetic}


def synthetic_bar(day, prev_close, close, volume_wan):
    """A reconstructed session: open at the prior close, 1.5% wicks on both sides."""
    high = round(max(prev_close, close) * 1.015, 2)
    low = round(min(prev_close, close) * 0.985, 2)
    return bar(day, prev_close, high, low, close, volume_wan, synthetic=True)


def sessions(count, end="2026-09-18"):
    day, out = date.fromisoformat(end), []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(out))


def series_from_closes(closes, *, end="2026-09-18", volume_wan=3000):
    days = sessions(len(closes), end)
    bars, prev = [], closes[0] * 0.97
    for day, close in zip(days, closes, strict=True):
        bars.append(synthetic_bar(day, round(prev, 2), close, volume_wan))
        prev = close
    return bars


# --- 神奇制药 600613.SH -------------------------------------------------------
# (date, close, volume in 万股); everything before 09-09 is reconstructed.
SHENQI_SYNTHETIC = [
    ("2026-08-13", 5.30, 4200), ("2026-08-14", 5.83, 5100), ("2026-08-17", 6.41, 6300),
    ("2026-08-18", 7.05, 7400), ("2026-08-19", 7.76, 8200), ("2026-08-20", 8.54, 9100),
    ("2026-08-21", 8.20, 10500), ("2026-08-24", 9.02, 9800), ("2026-08-25", 9.92, 11200),
    ("2026-08-26", 10.60, 13400), ("2026-08-27", 11.50, 15100), ("2026-08-28", 12.40, 16800),
    ("2026-08-31", 11.16, 14200), ("2026-09-01", 10.60, 12600), ("2026-09-02", 10.04, 11800),
    ("2026-09-03", 9.09, 13900), ("2026-09-04", 9.55, 10200), ("2026-09-07", 9.80, 9400),
    ("2026-09-08", 9.62, 8800),
]
SHENQI_REAL = [
    ("2026-09-09", 9.70, 10.01, 8.93, 9.02, 12854),
    ("2026-09-10", 8.77, 9.01, 8.20, 8.51, 11224),
    ("2026-09-11", 8.42, 8.52, 8.03, 8.10, 6967),
    ("2026-09-14", 8.08, 8.65, 7.92, 8.53, 7841),
    ("2026-09-15", 8.50, 8.63, 8.20, 8.23, 5838),
    ("2026-09-16", 8.15, 8.49, 8.04, 8.20, 4827),
    ("2026-09-17", 8.27, 9.02, 8.13, 8.54, 11288),
    ("2026-09-18", 8.12, 8.70, 8.12, 8.41, 9957),
]


def shenqi_bars():
    bars, prev = [], SHENQI_SYNTHETIC[0][1] * 0.94
    for day, close, volume in SHENQI_SYNTHETIC:
        bars.append(synthetic_bar(day, round(prev, 2), close, volume))
        prev = close
    for day, open_, high, low, close, volume in SHENQI_REAL:
        bars.append(bar(day, open_, high, low, close, volume))
    return bars


SHENQI_POSITION = {
    "snapshot_id": "broker-snapshot-2026-09-18", "observed_at": "2026-09-18T15:05:00+08:00",
    "quantity": 5800, "sellable_quantity": 5800, "average_cost": "8.4907",
    "market_price": "8.41", "market_value": "48778.00",
}
SHENQI_SECTOR = {"code": "881140", "name": "化学制药", "taxonomy": "longhu_ths_industry"}

# The same history with a rally on the plan day: the close sits more than half
# an ATR above MA5 and the crash low leaves room below, so this is the fixture
# on which a soft stop is *allowed* (the plain 09-18 close is too near MA5).
RALLY_BAR = bar("2026-09-18", 8.27, 9.00, 8.20, 8.90, 13000)
MID_AUTUMN_GAP = {"last_trading_date": "2026-09-24", "resume_date": "2026-09-28", "closed_days": 3}


def rally_bars():
    bars = shenqi_bars()
    bars[-1] = RALLY_BAR
    return bars


def shenqi_inputs(**overrides):
    payload = {
        "run_id": "run-2026-09-18-01", "account_key": "citics-primary", "symbol": "600613.SH",
        "name": "神奇制药", "as_of": AS_OF, "bars": shenqi_bars(), "equity": Decimal("99632"),
        "cash": Decimal("125.72"), "position": SHENQI_POSITION, "sector": SHENQI_SECTOR,
        "lane": {"lane": "reclaim", "formal_state": "observing", "run_id": "post-close-2026-09-18"},
        "calendar": CalendarInfo(upcoming_trading_dates=UPCOMING, closure_gaps=[]),
        "previous_plan": None,
    }
    payload.update(overrides)
    return GenerationInputs(**payload)


def rally_inputs(**overrides):
    return shenqi_inputs(bars=rally_bars(), **overrides)


# --- one realistic series per stage -------------------------------------------
STAGE_CLOSES = {
    "broken": [12.00, 11.90, 11.70, 11.60, 11.55, 11.40, 11.30, 11.20, 11.05, 11.00,
               11.10, 10.95, 10.85, 10.80, 10.70, 10.65, 10.60, 10.50, 10.45, 10.40,
               10.35, 10.45, 10.30, 10.25, 10.20, 10.15, 10.10, 10.30, 10.25, 10.20],
    "breakout_hold": [10.10, 9.95, 10.05, 10.00, 9.90, 10.05, 10.10, 9.95, 10.00, 10.05,
                      9.98, 10.02, 10.08, 9.96, 10.04, 10.00, 9.94, 10.06, 10.02, 9.98,
                      10.05, 10.00, 10.10, 10.02, 9.98, 10.20, 10.10, 10.30, 10.20, 11.00],
    "trend_hold": [10.00, 10.10, 10.25, 10.35, 10.50, 10.70, 10.85, 11.00, 11.20, 11.35,
                   11.50, 11.65, 11.80, 11.95, 12.05, 12.20, 12.35, 12.45, 12.55, 12.65,
                   12.75, 12.80, 12.90, 12.95, 13.00, 12.95, 12.90, 12.92, 12.96, 12.94],
    "pullback_hold": [8.00, 8.10, 8.20, 8.35, 8.50, 8.60, 8.75, 8.85, 8.95, 9.00,
                      9.20, 9.50, 9.80, 10.10, 10.40, 10.70, 11.00, 11.40, 11.80, 12.20,
                      11.40, 11.00, 10.80, 10.90, 11.00, 11.20, 11.50, 11.60, 11.45, 11.50],
    "base_platform": [10.30, 10.28, 10.32, 10.30, 10.26, 10.34, 10.30, 10.28, 10.32, 10.30,
                      9.90, 9.92, 9.88, 9.90, 9.94, 9.86, 9.90, 9.92, 9.88, 9.90,
                      10.15, 10.10, 10.05, 10.00, 9.98, 10.02, 10.06, 10.04, 10.00, 10.02],
    "unclassified": [14.00, 13.80, 13.60, 13.40, 13.20, 13.10, 13.30, 13.20, 13.10, 13.05,
                     13.00, 12.70, 12.40, 12.10, 11.80, 11.50, 11.30, 11.10, 10.90, 10.80,
                     10.60, 10.30, 10.10, 9.90, 10.00, 10.40, 10.80, 11.20, 11.00, 10.90],
}


def synthetic_calibration(*, stage_cap, holiday_cap, q99=12.0, holiday_q99=9.0):
    """A calibration artifact with the same shape as exposure_calibration.json, one cap for every cell."""
    from app.trade_discipline.exposure_calibration import BOARD_LABEL
    from app.trade_discipline.stage import STAGES

    def cells(cap, q99_pct):
        out = {f"{stage}|{board}": {"samples": 1000, "q99_loss_pct": q99_pct, "q95_loss_pct": q99_pct / 2,
                                    "cap_pct": cap, "fallback": None, "source_cell": f"{stage}|{board}"}
               for stage in STAGES for board in BOARD_LABEL}
        out.update({f"*|{board}": {"samples": 7000, "q99_loss_pct": q99_pct, "q95_loss_pct": q99_pct / 2,
                                   "cap_pct": cap, "fallback": None, "source_cell": f"*|{board}"}
                    for board in BOARD_LABEL})
        return out
    return {"version": f"synthetic:{stage_cap}:{holiday_cap}", "tolerance_pct": 5.0, "percentile": 99.0,
            "horizon_sessions": 2, "data_window": {"first_date": "2023-08-15", "last_date": "2026-09-18"},
            "stage_cells": cells(stage_cap, q99), "holiday_cells": cells(holiday_cap, holiday_q99)}


@contextmanager
def calibrated(calibration):
    """Point the generator at a synthetic calibration for the duration of the block."""
    from app.trade_discipline import generator as generator_module
    with patch.object(generator_module, "load_calibration", lambda: calibration):
        yield


def stage_inputs(stage, **overrides):
    closes = STAGE_CLOSES[stage]
    payload = {
        "run_id": f"run-{stage}", "account_key": "citics-primary", "symbol": "600000.SH",
        "name": f"样本-{stage}", "as_of": AS_OF, "bars": series_from_closes(closes),
        "equity": Decimal("200000"), "position": {
            "snapshot_id": f"snap-{stage}", "observed_at": "2026-09-18T15:05:00+08:00",
            "quantity": 3000, "sellable_quantity": 3000,
            "average_cost": str(round(closes[-1] * 0.92, 2)),
        },
        "sector": SHENQI_SECTOR,
        "calendar": CalendarInfo(upcoming_trading_dates=UPCOMING, closure_gaps=[]),
    }
    payload.update(overrides)
    return GenerationInputs(**payload)


class StageClassificationTests(unittest.TestCase):
    def test_crash_rebound_branch(self):
        metrics = daily_metrics(shenqi_bars())
        decision = classify_stage(metrics, {"lane": "reclaim"})
        self.assertEqual(decision["stage"], "crash_rebound")
        self.assertLessEqual(metrics["drawdown_pct"], -25.0)
        self.assertLess(metrics["ma5"], metrics["ma10"])
        self.assertEqual(decision["lane"], "reclaim")

    def test_broken_branch(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["broken"]))
        self.assertEqual(classify_stage(metrics)["stage"], "broken")
        self.assertLess(metrics["close"], metrics["ma20"])
        self.assertLess(metrics["ma5"], metrics["ma10"])
        self.assertGreater(metrics["drawdown_pct"], -25.0)

    def test_breakout_hold_branch(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["breakout_hold"]))
        self.assertEqual(classify_stage(metrics)["stage"], "breakout_hold")
        self.assertGreater(metrics["close"], metrics["prior_high"])

    def test_trend_hold_branch(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["trend_hold"]))
        self.assertEqual(classify_stage(metrics)["stage"], "trend_hold")
        self.assertGreaterEqual(metrics["ma5"], metrics["ma10"])
        self.assertGreaterEqual(metrics["ma10"], metrics["ma20"])
        self.assertGreater(metrics["drawdown_pct"], -5.0)

    def test_pullback_hold_branch(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["pullback_hold"]))
        self.assertEqual(classify_stage(metrics)["stage"], "pullback_hold")
        self.assertTrue(-15.0 <= metrics["drawdown_pct"] <= -3.0)
        self.assertGreaterEqual(metrics["close"], metrics["ma10"])

    def test_base_platform_branch(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["base_platform"]))
        self.assertEqual(classify_stage(metrics)["stage"], "base_platform")
        self.assertLessEqual(metrics["range10_pct"], 8.0)

    def test_unclassified_branch_uses_the_most_conservative_template(self):
        metrics = daily_metrics(series_from_closes(STAGE_CLOSES["unclassified"]))
        decision = classify_stage(metrics)
        self.assertEqual(decision["stage"], "unclassified")

    def test_lane_attribution_never_overrides_the_data_branch(self):
        metrics = daily_metrics(shenqi_bars())
        for lane in ({"lane": "trend"}, {"lane": "relay"}, None):
            self.assertEqual(classify_stage(metrics, lane)["stage"], "crash_rebound")

    def test_malformed_bars_are_dropped_rather_than_repaired(self):
        bars = shenqi_bars()
        bars.append({"trading_date": "2026-09-19", "open": 8.4, "high": 8.2, "low": 8.6, "close": 8.5})
        self.assertEqual(len(normalize_bars(bars)), len(bars) - 1)


class StageTemplateQualityTests(unittest.TestCase):
    def test_every_stage_template_passes_the_quality_gate(self):
        for stage in STAGE_CLOSES:
            with self.subTest(stage=stage):
                plan = generate(stage_inputs(stage))
                self.assertEqual(plan.stage, stage)
                self.assertEqual(plan.status, "active", failed_checks(plan.quality))
                self.assertTrue(plan.lines_of("hard_stop"))
                self.assertTrue(plan.lines_of("time_stop"))

    def test_crash_rebound_template_passes_on_the_real_fixture(self):
        plan = generate(shenqi_inputs())
        self.assertEqual(plan.stage, "crash_rebound")
        self.assertEqual(plan.status, "active", failed_checks(plan.quality))

    def test_hard_stop_carries_a_single_condition_and_an_intraday_copy(self):
        plan = generate(shenqi_inputs())
        hard = plan.lines_of("hard_stop")
        self.assertEqual(len(hard), 2)
        self.assertEqual({line.confirm.basis for line in hard}, {"daily", "minute"})
        self.assertEqual({line.price for line in hard}, {hard[0].price})
        for line in hard:
            self.assertEqual(line.extra, [])
        minute = next(line for line in hard if line.confirm.basis == "minute")
        self.assertEqual(minute.confirm.bars, 3)

    def test_crash_and_broken_stages_always_block_adding(self):
        for stage, inputs in (("crash_rebound", shenqi_inputs()), ("broken", stage_inputs("broken"))):
            with self.subTest(stage=stage):
                plan = generate(inputs)
                self.assertEqual(plan.stage, stage)
                no_add = plan.lines_of("no_add")
                self.assertEqual(len(no_add), 1)
                self.assertEqual(no_add[0].action.type, "block_add")

    def test_time_stop_is_three_sessions_for_crash_and_five_otherwise(self):
        self.assertEqual(generate(shenqi_inputs()).lines_of("time_stop")[0].trading_days, 3)
        self.assertEqual(generate(stage_inputs("trend_hold")).lines_of("time_stop")[0].trading_days, 5)

    def test_exposure_line_executes_by_time_not_by_price(self):
        plan = generate(shenqi_inputs(risk_per_trade_pct=Decimal("1.0")))
        exposure = plan.lines_of("exposure")[0]
        self.assertEqual((exposure.execute_by, exposure.execute_at), ("time", "next_open+15m"))
        self.assertIsNone(exposure.price)
        self.assertEqual(exposure.action.value, plan.sizing.max_shares)
        self.assertEqual(exposure.priority, 0)

    def test_holiday_line_appears_only_when_the_window_contains_a_long_closure(self):
        self.assertEqual(generate(shenqi_inputs()).lines_of("holiday"), [])
        calendar = CalendarInfo(upcoming_trading_dates=[date(2026, 9, 28), date(2026, 9, 29),
                                                        date(2026, 9, 30), date(2026, 10, 9),
                                                        date(2026, 10, 12)],
                               closure_gaps=[NATIONAL_DAY_GAP])
        with calibrated(synthetic_calibration(stage_cap=25, holiday_cap=10)):
            plan = generate(shenqi_inputs(calendar=calendar))
        holiday = plan.lines_of("holiday")[0]
        self.assertEqual(holiday.execute_by, "time")
        self.assertEqual(holiday.execute_at, "2026-09-30_before_close")
        # crash_rebound keeps half its 20% target over the break: 10% of 99632 at 8.41 = 1100 shares
        self.assertEqual(holiday.derivation.inputs["holiday_exposure_pct"], 10.0)
        self.assertEqual(holiday.action.value, 1100)
        self.assertEqual(plan.status, "active", failed_checks(plan.quality))

    def test_a_three_day_festival_weekend_never_forces_a_holiday_line(self):
        """Mid-Autumn 2026: Friday 09-25 closed plus the weekend is a 3-day gap, not a long closure."""
        calendar = CalendarInfo(upcoming_trading_dates=[date(2026, 9, 21), date(2026, 9, 22),
                                                        date(2026, 9, 23), date(2026, 9, 24),
                                                        date(2026, 9, 28)],
                               closure_gaps=[MID_AUTUMN_GAP])
        for stage_name, inputs in (("crash_rebound", shenqi_inputs(calendar=calendar)),
                                   ("broken", stage_inputs("broken", calendar=calendar))):
            with self.subTest(stage=stage_name):
                plan = generate(inputs)
                self.assertEqual(plan.stage, stage_name)
                self.assertEqual(plan.lines_of("holiday"), [])
                self.assertFalse(plan.metrics["closure_required"])
                self.assertIsNone(plan.metrics["closure"])
                verdict = {check.check_id: check for check in plan.quality}["holiday_line_when_closure"]
                self.assertTrue(verdict.passed, verdict.detail)
                # the refusal is on record: the gap, the threshold and the reason, not a silence
                self.assertIn(MID_AUTUMN_GAP, plan.metrics["calendar"]["closure_gaps"])
                omitted = [item for item in plan.metrics["omitted_lines"] if item["kind"] == "holiday"]
                self.assertEqual(len(omitted), 1)
                self.assertEqual(omitted[0]["inputs"], {"closed_days": 3, "last_trading_date": "2026-09-24",
                                                        "resume_date": "2026-09-28",
                                                        "threshold_days": HOLIDAY_CLOSURE_DAYS})
                self.assertIn("休市 3 个自然日", omitted[0]["reason"])
                self.assertIn(f"少于 {HOLIDAY_CLOSURE_DAYS} 个自然日", omitted[0]["reason"])

    def test_an_ordinary_weekend_leaves_no_holiday_record_at_all(self):
        """Saturday+Sunday after a Friday session is not a closure the rule ever considers."""
        calendar = CalendarInfo(upcoming_trading_dates=UPCOMING, closure_gaps=[WEEKEND_GAP])
        plan = generate(shenqi_inputs(calendar=calendar))
        self.assertEqual([item["kind"] for item in plan.metrics["omitted_lines"]], ["soft_stop"])
        self.assertTrue(is_ordinary_weekend(WEEKEND_GAP))
        self.assertFalse(is_ordinary_weekend(MID_AUTUMN_GAP))
        # a two-day closure that is not a weekend (a Wednesday session followed by two closed days) is recorded
        midweek = {"last_trading_date": "2026-09-23", "resume_date": "2026-09-26", "closed_days": 2}
        self.assertFalse(is_ordinary_weekend(midweek))
        self.assertEqual(closures_within({"closure_gaps": [WEEKEND_GAP, MID_AUTUMN_GAP, NATIONAL_DAY_GAP]},
                                         "2026-09-25"), [WEEKEND_GAP, MID_AUTUMN_GAP])

    def test_a_national_day_closure_uses_the_event_cap_only_when_current_holdings_exceed_it(self):
        """Holiday event risk is independent of the ordinary concentration stress reference."""
        calendar = CalendarInfo(upcoming_trading_dates=[date(2026, 9, 28), date(2026, 9, 29),
                                                        date(2026, 9, 30), date(2026, 10, 9),
                                                        date(2026, 10, 12)],
                               closure_gaps=[NATIONAL_DAY_GAP])
        tight = synthetic_calibration(stage_cap=25, holiday_cap=10)
        with calibrated(tight):
            plan = generate(shenqi_inputs(calendar=calendar))
        holiday = plan.lines_of("holiday")
        self.assertEqual(len(holiday), 1, failed_checks(plan.quality))
        self.assertEqual(holiday[0].derivation.inputs["holiday_exposure_pct"], 10.0)
        self.assertEqual(holiday[0].derivation.inputs["closed_days"], 8)
        self.assertEqual(holiday[0].action.value,
                         min(plan.sizing.current_shares,
                             lot_shares(plan.sizing.equity, Decimal("10"), plan.sizing.reference_price)))
        self.assertLessEqual(abs(holiday[0].derivation.recompute() - holiday[0].action.value), 0.01)
        self.assertIn("休市后两日99%跌幅", holiday[0].label)
        self.assertEqual(plan.status, "active", failed_checks(plan.quality))
        # A looser event cap still acts while current holdings exceed it; it is
        # no longer compared with the ordinary concentration stress reference.
        with calibrated(synthetic_calibration(stage_cap=25, holiday_cap=30)):
            loose = generate(shenqi_inputs(calendar=calendar))
        self.assertEqual(loose.lines_of("holiday")[0].action.value, 3500)
        # At 50% this fixture is already inside the event reference; do not
        # emit a no-op reduction, but keep the refusal auditable.
        with calibrated(synthetic_calibration(stage_cap=25, holiday_cap=50)):
            within = generate(shenqi_inputs(calendar=calendar))
        self.assertEqual(within.lines_of("holiday"), [])
        refusal = {item["kind"]: item for item in within.metrics["omitted_lines"]}["holiday"]
        self.assertIn("当前 5800 股未超过休市事件参考", refusal["reason"])
        self.assertEqual(refusal["inputs"]["holiday_cap_pct"], 50)
        self.assertEqual(within.status, "active", failed_checks(within.quality))

    def test_a_soft_stop_is_only_drawn_with_half_an_atr_on_each_side(self):
        """09-18 closed 8.41 with a 0.66 stop distance under one ATR: no price has half an ATR on both sides."""
        plain = generate(shenqi_inputs())
        self.assertEqual(plain.lines_of("soft_stop"), [])
        omitted = {item["kind"]: item for item in plain.metrics["omitted_lines"]}
        self.assertIn("soft_stop", omitted)
        self.assertIn("软止损区间为空", omitted["soft_stop"]["reason"])
        self.assertEqual(omitted["soft_stop"]["inputs"]["ma5"], plain.metrics["ma5"])
        self.assertEqual(omitted["soft_stop"]["inputs"]["hard_stop"], float(plain.sizing.hard_stop))
        self.assertEqual(plain.status, "active", failed_checks(plain.quality))

        rally = generate(rally_inputs())
        self.assertEqual(rally.stage, "crash_rebound")
        soft = rally.lines_of("soft_stop")
        self.assertEqual(len(soft), 1)
        atr14 = rally.metrics["atr14"]
        hard, reference = float(rally.sizing.hard_stop), float(rally.sizing.reference_price)
        self.assertGreaterEqual(float(soft[0].price) + 1e-9, hard + 0.5 * atr14)
        self.assertLessEqual(float(soft[0].price) - 1e-9, reference - 0.5 * atr14)
        self.assertNotIn("soft_stop", {item["kind"] for item in rally.metrics["omitted_lines"]})
        self.assertEqual(rally.status, "active", failed_checks(rally.quality))

    def test_the_soft_stop_refusal_names_an_empty_window_and_an_outside_ma5_differently(self):
        """An empty window (stop nearer than one ATR) and a real window that MA5 misses are two refusals."""
        plain = generate(shenqi_inputs())
        empty = {item["kind"]: item for item in plain.metrics["omitted_lines"]}["soft_stop"]
        hard, reference, atr14 = plain.sizing.hard_stop, plain.sizing.reference_price, plain.metrics["atr14"]
        self.assertGreater(empty["inputs"]["window_low"], empty["inputs"]["window_high"])
        self.assertEqual(empty["reason"],
                         f"软止损区间为空（下限 {empty['inputs']['window_low']:.2f} > 上限 "
                         f"{empty['inputs']['window_high']:.2f}，止损距离 {reference - hard} < 1.0×ATR14 "
                         f"{Decimal(str(atr14)).quantize(Decimal('0.01'))}），不生成")
        self.assertNotIn("不在", empty["reason"])
        # a close of 8.70 leaves a real window [8.28, 8.34] under MA5 8.44: the window exists, MA5 is outside
        bars = shenqi_bars()
        bars[-1] = bar("2026-09-18", 8.27, 8.75, 8.20, 8.70, 9000)
        outside_plan = generate(shenqi_inputs(bars=bars))
        self.assertEqual(outside_plan.stage, "crash_rebound")
        self.assertEqual(outside_plan.lines_of("soft_stop"), [])
        outside = {item["kind"]: item for item in outside_plan.metrics["omitted_lines"]}["soft_stop"]
        self.assertLessEqual(outside["inputs"]["window_low"], outside["inputs"]["window_high"])
        self.assertNotIn("区间为空", outside["reason"])
        self.assertIn(f"MA5 {Decimal(str(outside['inputs']['ma5'])).quantize(Decimal('0.01'))} 不在", outside["reason"])
        self.assertIn(f"[{outside['inputs']['window_low']:.2f}, {outside['inputs']['window_high']:.2f}] 内",
                      outside["reason"])
        self.assertIn("间距不足", outside["reason"])
        self.assertTrue(float(outside["inputs"]["ma5"]) > outside["inputs"]["window_high"])

    def test_soft_stop_separation_is_the_same_rule_in_the_template_and_the_gate(self):
        window_low, window_high = soft_stop_window(Decimal("7.75"), Decimal("8.41"), 0.73)
        self.assertEqual((window_low, window_high), (Decimal("8.12"), Decimal("8.05")))  # empty: no soft stop
        window_low, window_high = soft_stop_window(Decimal("7.92"), Decimal("8.90"), 0.746)
        self.assertLess(window_low, window_high)

    def test_the_trail_is_omitted_when_it_would_not_move_the_stop(self):
        """A flat-target stage and a trail no higher than the hard stop both say nothing."""
        with calibrated(synthetic_calibration(stage_cap=0, holiday_cap=0)):
            flat = generate(stage_inputs("broken"))
        flat_reason = {item["kind"]: item for item in flat.metrics["omitted_lines"]}["trail"]["reason"]
        self.assertIn("目标仓位为 0%", flat_reason)
        for stage_name in ("breakout_hold",):
            with self.subTest(stage=stage_name):
                plan = generate(stage_inputs(stage_name))
                self.assertEqual(plan.stage, stage_name)
                self.assertEqual(plan.lines_of("trail"), [])
                omitted = {item["kind"]: item for item in plan.metrics["omitted_lines"]}
                self.assertIn("trail", omitted)
                self.assertIn("trail_stop", omitted["trail"]["inputs"])
                self.assertEqual(plan.status, "active", failed_checks(plan.quality))
        breakout = generate(stage_inputs("breakout_hold"))
        reason = {item["kind"]: item for item in breakout.metrics["omitted_lines"]}["trail"]
        self.assertIn("不高于硬止损", reason["reason"])
        self.assertLessEqual(reason["inputs"]["trail_stop"], float(breakout.sizing.hard_stop))

    def test_the_trail_target_has_its_own_derivation(self):
        plan = generate(shenqi_inputs())
        trail = plan.lines_of("trail")[0]
        self.assertEqual(trail.derivation.action_formula, "max(floor_price, anchor_price)")
        self.assertEqual(set(trail.derivation.action_inputs), {"anchor_price", "anchor_source", "floor_price"})
        self.assertLessEqual(abs(trail.derivation.recompute_action() - float(trail.action.value)), 0.01)
        self.assertLessEqual(abs(trail.derivation.recompute() - float(trail.price)), 0.01)
        ratcheted = generate(shenqi_inputs(previous_plan={"plan_id": "prev", "hard_stop": 7.60, "trail": 8.50}))
        trail = ratcheted.lines_of("trail")[0]
        self.assertEqual(trail.action.value, Decimal("8.50"))
        self.assertEqual(trail.derivation.action_formula, "max(previous_trail, max(floor_price, anchor_price))")
        self.assertEqual(trail.derivation.action_inputs["previous_trail"], 8.5)
        self.assertLessEqual(abs(trail.derivation.recompute_action() - 8.5), 0.01)
        self.assertIn("前序移动止损8.50", trail.label)
        self.assertEqual(ratcheted.status, "active", failed_checks(ratcheted.quality))

    def test_the_trail_moves_the_stop_to_break_even_not_to_a_fraction_of_the_arm_price(self):
        """The 600613 defect: after an 11% rally the old trail parked the stop at 8.04, under the 8.49 cost."""
        plan = generate(shenqi_inputs())
        trail = plan.lines_of("trail")[0]
        cost = Decimal(SHENQI_POSITION["average_cost"])
        self.assertEqual(trail.action.type, "move_stop_to")
        self.assertEqual(trail.action.value, cost.quantize(Decimal("0.01")))          # 8.49, the average cost
        self.assertGreater(trail.action.value, plan.sizing.hard_stop)
        self.assertGreater(trail.action.value, Decimal(str(plan.metrics["low3"])))     # no longer the 3-day low
        self.assertEqual(trail.derivation.action_inputs["anchor_price"], float(cost))
        self.assertEqual(trail.derivation.action_inputs["anchor_source"], "average_cost")
        self.assertEqual(trail.derivation.action_inputs["floor_price"], float(plan.sizing.hard_stop))
        self.assertIn("把止损上移到成本价8.49", trail.label)
        self.assertIn("成本/现价孰高 + 1×ATR14", trail.label)
        # a new-buy plan anchors on the entry price it is sized on: max(lane reference 10.45, close 11.00)
        new_buy = generate(stage_inputs("breakout_hold", position=None,
                                        lane={"lane": "contraction", "reference": "10.45", "support": "9.90"}))
        trail = new_buy.lines_of("trail")[0]
        self.assertEqual(trail.action.value, Decimal("11.00"))
        self.assertEqual(trail.derivation.action_inputs["anchor_source"], "entry_price")
        self.assertIn("把止损上移到入场参考价11.00", trail.label)
        self.assertIn("入场参考价 + 1×ATR14", trail.label)
        self.assertEqual(new_buy.status, "active", failed_checks(new_buy.quality))
        # a holding whose cost already sits under the hard stop gains nothing from break-even: omitted
        profitable = generate(stage_inputs("trend_hold"))
        self.assertEqual(profitable.lines_of("trail"), [])
        reason = {item["kind"]: item for item in profitable.metrics["omitted_lines"]}["trail"]
        self.assertIn("保本目标 max(硬止损", reason["reason"])
        self.assertIn("不高于硬止损", reason["reason"])
        self.assertEqual(reason["inputs"]["anchor_source"], "average_cost")
        self.assertLess(reason["inputs"]["anchor_price"], float(profitable.sizing.hard_stop))

    def test_negative_broker_cost_is_displayable_but_not_a_price_anchor(self):
        position = PositionRef(
            snapshot_id="broker-snapshot", observed_at=AS_OF, quantity=100,
            sellable_quantity=100, average_cost=Decimal("-6.0287"),
            market_price=Decimal("8.25"), market_value=Decimal("825"),
        )
        self.assertEqual(position.average_cost, Decimal("-6.0287"))
        self.assertEqual(
            _anchor_price({"average_cost": position.average_cost}, Decimal("8.25"), "holding"),
            (Decimal("8.25"), "reference_price"),
        )

    def test_the_trail_is_confirmed_on_the_daily_close_like_every_other_daily_line(self):
        for plan in (generate(shenqi_inputs()), generate(rally_inputs())):
            trail = plan.lines_of("trail")[0]
            self.assertEqual((trail.metric, trail.op), ("daily_close", ">="))
            self.assertEqual((trail.confirm.bars, trail.confirm.basis), (1, "daily"))
            self.assertTrue(trail.label.startswith("日线收盘站上"))

    def test_the_take_partial_sentence_leads_with_the_conditions_that_carry_it(self):
        plan = generate(shenqi_inputs())
        partial = plan.lines_of("take_partial")[0]
        self.assertEqual(partial.extra, ["after_volume_climax", "below_vwap"])
        self.assertEqual(partial.label,
                         "当日成交量为20日最大量且收在振幅下半且最新价跌破当日VWAP、且最新价低于8.41时，减半仓")
        self.assertLess(partial.label.index("VWAP"), partial.label.index("最新价低于8.41"))
        self.assertNotIn("下方减半仓", partial.label)

    def test_new_buy_plan_adds_a_trigger_and_a_cancel_line(self):
        plan = generate(stage_inputs("breakout_hold", position=None,
                                     lane={"lane": "contraction", "reference": "10.45", "support": "9.90"}))
        self.assertEqual(plan.plan_kind, "new_buy")
        # trigger floor = max(lane reference 10.45, hard stop 10.24 + 0.5 x ATR14) = 10.46 (the stop gap binds)
        self.assertEqual(plan.lines_of("trigger")[0].price, Decimal("10.46"))
        self.assertEqual(plan.lines_of("trigger")[0].derivation.inputs["binding_term"], "stop_gap")
        self.assertEqual(plan.lines_of("cancel")[0].price, Decimal("9.90"))
        self.assertIn("amount_ge_prev_day", plan.lines_of("trigger")[0].extra)
        self.assertEqual(plan.status, "active", failed_checks(plan.quality))

    def test_every_line_price_recomputes_from_its_own_derivation(self):
        plan = generate(shenqi_inputs())
        for line in plan.lines:
            with self.subTest(kind=line.kind, rule=line.derivation.rule_id):
                self.assertTrue(line.derivation.inputs)
                self.assertTrue(line.derivation.formula)
                if line.price is not None:
                    self.assertLessEqual(abs(line.derivation.recompute() - float(line.price)), 0.01)
                elif line.action.value is not None:   # exposure / holiday: the formula is the share count
                    self.assertLessEqual(abs(line.derivation.recompute() - float(line.action.value)), 0.01)
                if line.action.type == "move_stop_to":
                    self.assertLessEqual(abs(line.derivation.recompute_action() - float(line.action.value)), 0.01)


class SizingTests(unittest.TestCase):
    def test_sizing_formula_is_driven_by_the_stop_distance(self):
        sizing = build_sizing(stage="crash_rebound", equity=Decimal("99632"),
                              risk_per_trade_pct=Decimal("1.0"), reference_price=Decimal("8.41"),
                              hard_stop=Decimal("7.75"), current_shares=5800, cap_pct=20)
        self.assertEqual(sizing.risk_amount, Decimal("996.32"))
        self.assertEqual(sizing.stop_distance, Decimal("0.66"))
        self.assertEqual(sizing.max_shares, 1500)          # floor(996.32 / 0.66 / 100) * 100
        self.assertEqual(sizing.target_exposure_pct, Decimal("20"))
        self.assertEqual(sizing.recommended_shares, 1500)  # tighter than the 20% exposure cap
        self.assertEqual(sizing.cap_shares, 2300)          # floor(99632 x 20% / 8.41 / 100) x 100
        self.assertEqual(sizing.binding_constraint, "risk")
        self.assertEqual(sizing.current_exposure_pct, Decimal("48.96"))
        self.assertEqual(sizing.current_risk_pct, Decimal("3.84"))   # 5800 x 0.66 / 99632

    def test_current_risk_pct_is_the_open_risk_of_the_snapshot_position(self):
        """The 600613 card: 5800 shares, stop distance 0.77 on 99632.26 equity = 4.48%."""
        sizing = build_sizing(stage="crash_rebound", equity=Decimal("99632.26"),
                              risk_per_trade_pct=Decimal("1.0"), reference_price=Decimal("8.41"),
                              hard_stop=Decimal("7.64"), current_shares=5800, cap_pct=20)
        self.assertEqual(sizing.current_risk_pct, Decimal("4.48"))
        self.assertGreater(sizing.current_risk_pct, sizing.risk_per_trade_pct)

    def test_tail_risk_reference_never_overrides_the_stop_risk_budget(self):
        sizing = build_sizing(stage="broken", equity=Decimal("99632"), risk_per_trade_pct=Decimal("1.0"),
                              reference_price=Decimal("8.41"), hard_stop=Decimal("7.75"),
                              current_shares=5800, cap_pct=5)
        self.assertEqual(sizing.target_exposure_pct, Decimal("5"))
        self.assertEqual(sizing.cap_shares, 500)           # floor(99632 x 5% / 8.41 / 100) x 100
        self.assertEqual(sizing.recommended_shares, 1500)  # tail reference is disclosure, not an order limit
        self.assertEqual(sizing.binding_constraint, "risk")
        self.assertEqual(sizing.concentration_policy, "tail_risk_advisory")

    def test_recommended_shares_always_round_down_to_whole_lots(self):
        sizing = build_sizing(stage="trend_hold", equity=Decimal("200000"),
                              risk_per_trade_pct=Decimal("1.0"), reference_price=Decimal("12.86"),
                              hard_stop=Decimal("12.50"), current_shares=0, cap_pct=45)
        self.assertEqual(sizing.recommended_shares % LOT_SIZE, 0)
        self.assertLessEqual(sizing.recommended_shares, sizing.max_shares)


class TrailTests(unittest.TestCase):
    def test_trail_never_moves_down(self):
        previous = Decimal("8.90")
        lowered = trail_stop_price(anchor_price=Decimal("8.49"), floor_price=Decimal("7.20"),
                                   previous_trail=previous)
        self.assertEqual(lowered, previous)
        raised = trail_stop_price(anchor_price=Decimal("9.80"), floor_price=Decimal("7.20"),
                                  previous_trail=previous)
        self.assertEqual(raised, Decimal("9.80"))
        self.assertGreater(raised, previous)

    def test_trail_never_sits_below_the_hard_stop(self):
        value = trail_stop_price(anchor_price=Decimal("6.00"), floor_price=Decimal("7.75"), previous_trail=None)
        self.assertEqual(value, Decimal("7.75"))

    def test_trail_is_the_anchor_rounded_to_the_tick(self):
        self.assertEqual(trail_stop_price(anchor_price=Decimal("8.4907"), floor_price=Decimal("7.75")),
                         Decimal("8.49"))
        self.assertEqual(trail_stop_price(anchor_price=Decimal("8.495"), floor_price=Decimal("7.75")),
                         Decimal("8.50"))

    def test_trail_is_monotonic_across_a_moving_anchor_and_floor(self):
        current, seen = None, []
        for anchor, floor_price in ((Decimal("8.49"), Decimal("7.75")), (Decimal("8.49"), Decimal("8.60")),
                                    (Decimal("8.20"), Decimal("7.90")), (Decimal("9.10"), Decimal("8.00")),
                                    (Decimal("8.49"), Decimal("7.40"))):
            current = trail_stop_price(anchor_price=anchor, floor_price=floor_price, previous_trail=current)
            seen.append(current)
        self.assertEqual(seen, sorted(seen))

    def test_trail_arm_price_sits_above_the_reference_price(self):
        plan = generate(shenqi_inputs())
        trail = plan.lines_of("trail")[0]
        self.assertGreater(trail.price, plan.sizing.reference_price)
        self.assertEqual(trail.action.type, "move_stop_to")
        self.assertGreaterEqual(trail.action.value, plan.sizing.hard_stop)


class QualityGateFailureTests(unittest.TestCase):
    """Every assertion in the gate needs one example that makes it fail."""

    def setUp(self):
        # the rally fixture carries every optional line, including the soft stop; a calibration whose
        # holiday cap (10%) is tighter than the stage cap (25%) keeps the holiday rule binding here
        with calibrated(synthetic_calibration(stage_cap=25, holiday_cap=10)):
            self.plan = generate(rally_inputs())
        self.assertEqual(self.plan.status, "active", failed_checks(self.plan.quality))
        self.assertTrue(self.plan.lines_of("soft_stop"))
        self.assertTrue(self.plan.lines_of("trail"))
        self.covered: set[str] = set()

    def verdicts(self, **update) -> dict[str, QualityCheck]:
        changed = self.plan.model_copy(update=update)
        return {check.check_id: check for check in evaluate_quality(changed)}

    def assert_fails(self, check_id: str, **update):
        verdicts = self.verdicts(**update)
        self.assertFalse(verdicts[check_id].passed, f"{check_id} should have failed: {verdicts[check_id].detail}")
        self.covered.add(check_id)

    def without(self, kind: str) -> list[Line]:
        return [line for line in self.plan.lines if line.kind != kind]

    def replace(self, kind: str, **update) -> list[Line]:
        return [line.model_copy(update=update) if line.kind == kind else line for line in self.plan.lines]

    def metrics_with(self, **update) -> dict:
        return {**self.plan.metrics, **update}

    def frozen_calendar(self, *gaps: dict) -> dict:
        """A frozen ``metrics.calendar`` whose sessions include every gap's last session."""
        sessions = sorted({*self.plan.metrics["calendar"]["sessions"], *(gap["last_trading_date"] for gap in gaps)})
        return {"closure_gaps": list(gaps), "sessions": sessions}

    def test_every_quality_assertion_has_a_failing_example(self):
        self.assert_fails("has_hard_stop", lines=self.without("hard_stop"))
        self.assert_fails("has_time_stop", lines=self.without("time_stop"))
        self.assert_fails("hard_stop_below_price", lines=self.replace("hard_stop", price=Decimal("9.99")))
        self.assert_fails("hard_stop_single_condition",
                          lines=self.replace("hard_stop", extra=["volume_expand_1_5x"]))
        self.assert_fails("hard_stop_distance_sane", lines=self.replace("hard_stop", price=Decimal("8.40")))
        self.assert_fails("soft_above_hard", lines=self.replace("soft_stop", price=Decimal("7.00")))
        crowded = self.plan.sizing.hard_stop + Decimal("0.05")   # inside half an ATR of the hard stop
        self.assert_fails("soft_stop_separation", lines=self.replace("soft_stop", price=crowded))
        self.assert_fails("lines_monotonic", lines=self.replace("soft_stop", price=Decimal("9.50")))
        self.assert_fails("every_line_evaluable", lines=self.replace("soft_stop", op=None))
        self.assert_fails("every_line_has_derivation",
                          lines=self.replace("time_stop",
                                             derivation=Derivation(rule_id="hand_written", inputs={}, formula="")))
        # Force a real stop-risk breach, then omit the required reduction.
        self.assert_fails(
            "exposure_line_when_over_risk",
            lines=self.without("exposure"),
            sizing=self.plan.sizing.model_copy(update={"current_shares": self.plan.sizing.max_shares + 100}),
        )
        self.assert_fails("holiday_line_when_closure",
                          metrics=self.metrics_with(calendar=self.frozen_calendar(IN_WINDOW_GAP)))
        self.assert_fails("no_add_when_crash_or_broken", lines=self.without("no_add"))
        self.assert_fails("sizing_consistent",
                          sizing=self.plan.sizing.model_copy(update={"recommended_shares": 5800}))
        self.assert_fails("not_lowered_vs_previous",
                          metrics=self.metrics_with(previous_hard_stop=8.10))
        self.assert_fails("valid_until_within_5_trading_days",
                          metrics=self.metrics_with(valid_until_limit="2026-09-22"))
        # a new buy sized on the lane's structure level (pre-v3, no metrics.entry)
        self.assert_fails("entry_reference_current", plan_kind="new_buy")
        self.assert_fails("buy_zone_valid", plan_kind="new_buy")      # no trigger / chase cap at all
        self.assertEqual(self.covered, set(CHECK_IDS))

    def test_a_sector_condition_without_a_stored_membership_is_not_evaluable(self):
        verdicts = self.verdicts(metrics=self.metrics_with(sector_available=False))
        self.assertFalse(verdicts["every_line_evaluable"].passed)

    def test_a_soft_stop_too_near_the_reference_price_fails_separation(self):
        """The 600613 defect: MA5 0.36% under the close is not a line, it is noise."""
        reference = self.plan.sizing.reference_price
        verdicts = self.verdicts(lines=self.replace("soft_stop", price=reference - Decimal("0.03")))
        self.assertFalse(verdicts["soft_stop_separation"].passed)
        self.assertIn("0.5×ATR14", verdicts["soft_stop_separation"].detail)

    def test_a_trail_whose_target_does_not_recompute_fails_the_derivation_gate(self):
        trail = self.plan.lines_of("trail")[0]
        wrong_value = self.replace("trail", action=Action(type="move_stop_to", value=trail.action.value + 1))
        self.assertFalse(self.verdicts(lines=wrong_value)["every_line_has_derivation"].passed)
        no_formula = self.replace("trail", derivation=trail.derivation.model_copy(
            update={"action_formula": "", "action_inputs": {}}))
        self.assertFalse(self.verdicts(lines=no_formula)["every_line_has_derivation"].passed)

    def test_an_exposure_line_whose_share_count_does_not_recompute_fails_the_derivation_gate(self):
        plan = generate(rally_inputs(risk_per_trade_pct=Decimal("1.0")))
        exposure = plan.lines_of("exposure")[0]
        wrong = [line.model_copy(update={"action": Action(type="reduce_to_shares", value=exposure.action.value + 100)})
                 if line.kind == "exposure" else line for line in plan.lines]
        verdicts = {check.check_id: check for check in evaluate_quality(plan.model_copy(update={"lines": wrong}))}
        self.assertFalse(verdicts["every_line_has_derivation"].passed)

    def test_the_holiday_gate_re_derives_the_closure_from_the_frozen_calendar_not_the_flag(self):
        """The generator's flag is ignored in both directions; the frozen calendar decides."""
        short = self.metrics_with(closure_required=True, closure=MID_AUTUMN_GAP,
                                  calendar=self.frozen_calendar(MID_AUTUMN_GAP))
        self.assertTrue(self.verdicts(metrics=short)["holiday_line_when_closure"].passed)
        long = self.metrics_with(closure_required=False, closure=None, calendar=self.frozen_calendar(IN_WINDOW_GAP))
        verdict = self.verdicts(metrics=long)["holiday_line_when_closure"]
        self.assertFalse(verdict.passed)
        self.assertIn("2026-09-22 起休市 5 个自然日", verdict.detail)
        # a closure whose last session lies beyond valid_until (09-25) does not bind this plan
        beyond = self.metrics_with(closure_required=True, calendar=self.frozen_calendar(NATIONAL_DAY_GAP))
        self.assertTrue(self.verdicts(metrics=beyond)["holiday_line_when_closure"].passed)

    def test_a_plan_stored_before_the_calendar_was_frozen_is_judged_by_its_recorded_closure(self):
        legacy = {key: value for key, value in self.plan.metrics.items() if key != "calendar"}
        self.assertTrue(self.verdicts(metrics={**legacy, "closure_required": True, "closure": MID_AUTUMN_GAP})
                        ["holiday_line_when_closure"].passed)
        self.assertFalse(self.verdicts(metrics={**legacy, "closure_required": True, "closure": NATIONAL_DAY_GAP})
                         ["holiday_line_when_closure"].passed)

    def test_a_lowered_hard_stop_is_allowed_only_with_a_recorded_reason(self):
        lowered = self.plan.model_copy(update={"metrics": self.metrics_with(previous_hard_stop=8.10),
                                               "lowered_reason": "前一计划止损位于除权前价格，已按复权口径下调"})
        self.assertTrue({check.check_id: check for check in evaluate_quality(lowered)}
                        ["not_lowered_vs_previous"].passed)

    def test_a_failing_plan_is_still_produced_and_marked_rejected(self):
        broken_bars = shenqi_bars()
        broken_bars[-1] = {**broken_bars[-1], "low": 8.41}  # no room between the close and any structure low
        plan = generate(shenqi_inputs(bars=broken_bars, calendar=CalendarInfo(upcoming_trading_dates=[])))
        self.assertEqual(plan.status, "rejected_by_quality")
        self.assertIn("valid_until_within_5_trading_days", failed_checks(plan.quality))
        self.assertTrue(plan.lines)
        self.assertTrue(plan.sizing)

    def test_quality_helpers_agree_with_the_plan_status(self):
        self.assertTrue(quality_passed(self.plan.quality))
        self.assertEqual(failed_checks(self.plan.quality), [])
        self.assertTrue(self.plan.quality_passed)

    def test_a_hand_written_line_without_a_metric_is_rejected(self):
        hand_written = Line(kind="soft_stop", label="跌破就减仓", metric=None, op=None,
                            price=None, action=Action(type="reduce_by_pct", value=Decimal("50")),
                            derivation=Derivation(rule_id="human.note", inputs={"note": 1}, formula="note"),
                            confirm=Confirm(), priority=3)
        verdicts = self.verdicts(lines=[*self.plan.lines, hand_written])
        self.assertFalse(verdicts["every_line_evaluable"].passed)


class ShenqiFixtureTests(unittest.TestCase):
    def setUp(self):
        self.plan = generate(shenqi_inputs())

    def test_plan_header_and_evidence(self):
        plan = self.plan
        self.assertEqual((plan.symbol, plan.name, plan.plan_kind), ("600613.SH", "神奇制药", "holding"))
        self.assertEqual(plan.stage, "crash_rebound")
        self.assertEqual(plan.trading_date, date(2026, 9, 18))
        self.assertEqual(plan.valid_until.date(), date(2026, 9, 25))
        self.assertEqual(plan.template_key, "crash_rebound@trade-discipline-templates-v7")
        self.assertEqual(plan.position.quantity, 5800)
        self.assertEqual(plan.metrics["t1_locked_shares"], 0)
        self.assertEqual(len(plan.inputs_hash), 64)
        self.assertIn("position_snapshot:broker-snapshot-2026-09-18", plan.evidence_refs)
        self.assertIn("sector:longhu_ths_industry:881140", plan.evidence_refs)
        self.assertTrue(any(ref.startswith("bars_synthetic:") for ref in plan.evidence_refs))

    def test_metrics_snapshot_matches_the_bars(self):
        metrics = self.plan.metrics
        self.assertEqual(metrics["close"], 8.41)
        self.assertEqual(metrics["hi20"], 12.40)
        self.assertAlmostEqual(metrics["ma5"], 8.382, places=3)
        self.assertAlmostEqual(metrics["ma10"], 8.696, places=3)
        self.assertAlmostEqual(metrics["drawdown_pct"], -32.18, places=2)
        self.assertEqual(metrics["low3"], 8.04)
        self.assertEqual(metrics["synthetic_bars"], len(SHENQI_SYNTHETIC))

    def test_hard_stop_comes_from_the_real_structure_low_and_stays_in_the_sane_band(self):
        price, derivation = hard_stop_price("crash_rebound", self.plan.metrics, Decimal("8.41"))
        self.assertEqual(derivation.rule_id, "hard_stop.crash_rebound")
        # crash_rebound anchors on the crash low: the lowest low of the last 20 sessions (09-14, 7.92)
        self.assertEqual(derivation.inputs["low20"], 7.92)
        self.assertEqual(self.plan.metrics["low20"], 7.92)
        self.assertEqual(derivation.inputs["structure_source"], "low20")
        self.assertNotIn("today_low", derivation.inputs)
        self.assertLessEqual(abs(derivation.recompute() - float(price)), 0.01)
        atr14 = self.plan.metrics["atr14"]
        distance = 8.41 - float(price)
        self.assertTrue(0.8 * atr14 <= distance <= 3 * atr14)
        self.assertTrue(0.015 <= distance / 8.41 <= 0.12)

    def test_the_hard_stop_formula_carries_all_four_contract_terms(self):
        _, derivation = hard_stop_price("crash_rebound", self.plan.metrics, Decimal("8.41"))
        for term in ("low20", "reference_price * (1 - buffer_pct)",
                     "reference_price - atr_target_multiple * atr14", "reference_price * (1 - stop_pct_target)"):
            self.assertIn(term, derivation.formula)
        self.assertEqual(derivation.inputs["stop_pct_target"], 0.02)
        self.assertEqual(derivation.inputs["atr_target_multiple"], 0.9)
        # structure 0.5% away, ATR term 0.45% away, buffer 1.5%: the 2% term is the one that binds
        calm = {**self.plan.metrics, "low20": 9.95, "atr14": 0.05, "volatility": 0.5}
        price, derivation = hard_stop_price("crash_rebound", calm, Decimal("10.00"))
        self.assertEqual(derivation.inputs["buffer_pct"], 0.015)
        self.assertEqual(price, Decimal("9.80"))
        self.assertLessEqual(abs(derivation.recompute() - 9.80), 0.01)

    def test_other_stages_keep_their_structure_points(self):
        metrics = {**self.plan.metrics, "low": 8.12, "prev_low": 8.13, "low20": 7.92}
        _, broken = hard_stop_price("broken", metrics, Decimal("8.41"))
        self.assertTrue(broken.formula.startswith("min(min(today_low, prev_low)"))
        self.assertEqual((broken.inputs["today_low"], broken.inputs["prev_low"]), (8.12, 8.13))
        self.assertNotIn("low20", broken.inputs)
        _, pullback = hard_stop_price("pullback_hold", metrics, Decimal("8.41"))
        self.assertTrue(pullback.formula.startswith("min(recent_low"))

    def test_sizing_reports_concentration_but_only_stop_risk_can_cut(self):
        sizing = self.plan.sizing
        self.assertEqual(sizing.equity, Decimal("99632"))
        self.assertEqual(sizing.current_shares, 5800)
        self.assertGreater(sizing.current_exposure_pct, sizing.target_exposure_pct)
        self.assertEqual(sizing.recommended_shares, sizing.max_shares)
        self.assertEqual(sizing.recommended_shares % LOT_SIZE, 0)

    def test_the_plan_states_every_required_line_kind(self):
        kinds = {line.kind for line in self.plan.lines}
        self.assertTrue({"hard_stop", "take_partial", "trail", "no_add", "time_stop"} <= kinds)
        self.assertNotIn("exposure", kinds)  # 5% risk budget is not breached; concentration alone is not actionable
        # the soft stop is refused on this fixture and the refusal is on record
        self.assertNotIn("soft_stop", kinds)
        self.assertEqual([item["kind"] for item in self.plan.metrics["omitted_lines"]], ["soft_stop"])
        self.assertEqual([line.priority for line in self.plan.lines],
                         sorted(line.priority for line in self.plan.lines))

    def test_generation_is_deterministic(self):
        again = generate(shenqi_inputs())
        self.assertEqual(again.inputs_hash, self.plan.inputs_hash)
        self.assertEqual(again.model_dump(mode="json"), self.plan.model_dump(mode="json"))

    def test_the_t1_lock_is_only_read_from_a_snapshot_taken_on_the_trading_date(self):
        """A 09-17 snapshot cannot describe what is locked on 09-18: everything it held is sellable by then."""
        same_day = generate(shenqi_inputs(position={**SHENQI_POSITION, "sellable_quantity": 0}))
        self.assertEqual(same_day.metrics["t1_locked_shares"], 5800)
        self.assertEqual(same_day.metrics["t1_snapshot_at"], "2026-09-18T15:05:00+08:00")
        stale = generate(shenqi_inputs(position={**SHENQI_POSITION, "sellable_quantity": 0,
                                                  "observed_at": "2026-09-17T15:10:00+08:00"}))
        self.assertEqual(stale.metrics["t1_locked_shares"], 0)
        self.assertEqual(stale.metrics["t1_snapshot_at"], "2026-09-17T15:10:00+08:00")
        self.assertEqual(stale.position.sellable_quantity, 0)          # the snapshot itself is still reported
        position = PositionRef(snapshot_id="s", observed_at=datetime(2026, 9, 18, 3, 10, tzinfo=ZoneInfo("UTC")),
                               quantity=5800, sellable_quantity=3000, average_cost=None, market_price=None,
                               market_value=None)
        self.assertEqual(t1_locked_shares_for(position, date(2026, 9, 18)), 2800)   # 11:10 Shanghai
        self.assertEqual(t1_locked_shares_for(position, date(2026, 9, 21)), 0)
        self.assertEqual(t1_locked_shares_for(None, date(2026, 9, 18)), 0)


class HardStopStructureTests(unittest.TestCase):
    """The hard stop is a structure point; nothing may lift it back into the range."""

    WIDE = {"low": 9.00, "prev_low": 8.80, "low20": 8.80, "recent_low": 9.10, "ma10": 9.50,
            "prior_high": 10.50, "low10_close": 8.90, "atr14": 0.30, "volatility": 1.0}

    def test_a_structure_wider_than_three_atr_is_kept_not_raised_into_the_range(self):
        price, derivation = hard_stop_price("broken", self.WIDE, Decimal("10.00"))
        self.assertEqual(price, Decimal("8.80"))                      # the two-day low, 1.2 = 4 x ATR away
        self.assertLess(price, Decimal(str(self.WIDE["low"])))        # never above today's own low
        self.assertNotIn("band_low)", derivation.formula)
        self.assertLessEqual(abs(derivation.recompute() - float(price)), 0.01)
        # crash_rebound with both structures beyond the band: the fallback is used and it is still too far
        price, derivation = hard_stop_price("crash_rebound", {**self.WIDE, "low20": 8.00}, Decimal("10.00"))
        self.assertEqual(derivation.inputs["structure_source"], "two_day_low")
        self.assertEqual(price, Decimal("8.80"))
        self.assertTrue(stop_beyond_band(Decimal("10.00"), price, self.WIDE["atr14"]))

    def test_crash_rebound_falls_back_to_the_two_day_low_once_the_crash_low_is_beyond_the_band(self):
        """The 600613 cliff: a normal rebound to 9.05 with low20 = 7.92 (12.5%) must not reject the card.

        At 9.30 the two-day low 8.13 is itself 12.6% away, so that plan is
        still rejected - by the contract's rule that a structure too far to
        size is kept and refused, not by the fixed crash low.
        """
        for close, source, status in ((8.41, "low20", "active"), (8.90, "low20", "active"),
                                      (9.00, "two_day_low", "active"), (9.05, "two_day_low", "active"),
                                      (9.30, "two_day_low", "rejected_by_quality")):
            with self.subTest(close=close):
                bars = shenqi_bars()
                bars[-1] = bar("2026-09-18", 8.27, max(9.02, close), 8.20, close, 13000)
                plan = generate(shenqi_inputs(bars=bars))
                self.assertEqual(plan.stage, "crash_rebound")
                self.assertEqual(plan.status, status, failed_checks(plan.quality))
                daily = next(line for line in plan.lines_of("hard_stop") if line.confirm.basis == "daily")
                self.assertEqual(daily.derivation.inputs["structure_source"], source)
                self.assertEqual(daily.derivation.inputs["low20"], 7.92)   # recorded either way
                self.assertLessEqual(abs(daily.derivation.recompute() - float(daily.price)), 0.01)
                if source == "two_day_low":
                    self.assertTrue(daily.derivation.formula.startswith("min(min(today_low, prev_low)"))
                    self.assertEqual((daily.derivation.inputs["today_low"], daily.derivation.inputs["prev_low"]),
                                     (8.20, 8.13))
                    self.assertLessEqual(daily.price, Decimal("8.13"))
                    self.assertIn(CRASH_FALLBACK_LABEL, daily.label)
                else:
                    self.assertTrue(daily.derivation.formula.startswith("min(low20"))
                    self.assertNotIn("today_low", daily.derivation.inputs)
                    self.assertNotIn("改用", daily.label)
        # the fallback decision and the gate share one arithmetic, so the boundary can never disagree
        self.assertFalse(stop_beyond_band(Decimal("8.90"), Decimal("7.92"), 0.747))
        self.assertTrue(stop_beyond_band(Decimal("9.00"), Decimal("7.92"), 0.747))

    def test_such_a_plan_is_rejected_by_the_distance_gate_rather_than_silently_retuned(self):
        """Principle 9: the gate says "this structure is too far to size", and it is stored."""
        closes = [10.6, 10.5, 10.45, 10.4, 10.3, 10.2, 10.15, 10.1, 10.05, 10.0,
                  9.95, 9.9, 9.85, 9.8, 9.75, 9.7, 9.65, 9.6, 9.55, 9.5,
                  9.45, 9.4, 9.35, 9.3, 9.25, 9.2, 9.15, 9.1, 9.05, 9.0]
        bars = series_from_closes(closes)
        bars[-1] = {**bars[-1], "low": 7.60}       # a real crash low far below the buffer
        plan = generate(stage_inputs("broken", bars=bars))
        self.assertLessEqual(float(plan.sizing.hard_stop), 7.60)
        self.assertEqual(plan.status, "rejected_by_quality")
        self.assertIn("hard_stop_distance_sane", failed_checks(plan.quality))

    def test_a_structure_too_close_may_still_be_widened_but_never_tightened(self):
        tight = {**self.WIDE, "low": 9.98, "prev_low": 9.97, "low20": 9.97}
        price, _ = hard_stop_price("crash_rebound", tight, Decimal("10.00"))
        self.assertLess(price, Decimal("9.97"))                       # widened below the structure
        self.assertLessEqual(float(price), 10.00 - 0.9 * tight["atr14"] + 0.01)

    def test_the_derivation_records_which_of_the_four_terms_bound(self):
        """``binding_term`` is the label's source: it must name the term that actually produced the price."""
        # 600613 on 09-18: low20 7.92 sits 0.49 above the close, the 0.9 x ATR14 term (0.66) pulls it down
        plan = generate(shenqi_inputs())
        daily = next(line for line in plan.lines_of("hard_stop") if line.confirm.basis == "daily")
        self.assertEqual(daily.derivation.inputs["binding_term"], "atr")
        self.assertEqual(daily.derivation.inputs["structure_value"], 7.92)
        self.assertLess(float(daily.price), 7.92)
        # the rally close 8.90 leaves the crash low as the nearest term: the structure binds
        rally = generate(rally_inputs())
        daily = next(line for line in rally.lines_of("hard_stop") if line.confirm.basis == "daily")
        self.assertEqual(daily.derivation.inputs["binding_term"], "structure")
        self.assertEqual(daily.price, Decimal("7.92"))
        # the calm fixture from the formula test: the flat 2% term binds; a wide buffer binds the fourth
        calm = {**plan.metrics, "low20": 9.95, "atr14": 0.05, "volatility": 0.5}
        _, derivation = hard_stop_price("crash_rebound", calm, Decimal("10.00"))
        self.assertEqual(derivation.inputs["binding_term"], "pct")
        volatile = {**plan.metrics, "low20": 9.95, "atr14": 0.05, "volatility": 9.0}   # buffer 5.4%
        _, derivation = hard_stop_price("crash_rebound", volatile, Decimal("10.00"))
        self.assertEqual(derivation.inputs["binding_term"], "buffer")
        self.assertEqual(HARD_STOP_TERMS, ("structure", "buffer", "atr", "pct"))

    def test_the_hard_stop_label_names_the_term_that_bound_not_a_static_structure_name(self):
        """Four cards all said 结构低点 while the stop was reference - 0.9 x ATR14; the label must say which."""
        widened = generate(shenqi_inputs())
        daily = next(line for line in widened.lines_of("hard_stop") if line.confirm.basis == "daily")
        self.assertEqual(daily.label,
                         "日线收盘跌破7.75即全部退出（急跌反弹段，结构点最近20个交易日最低价7.92"
                         "距离不足最小止损距离，按 0.9×ATR14 向下加宽）")
        anchored = generate(rally_inputs())
        daily = next(line for line in anchored.lines_of("hard_stop") if line.confirm.basis == "daily")
        self.assertEqual(daily.label, "日线收盘跌破7.92即全部退出（急跌反弹段，结构点：最近20个交易日最低价7.92）")
        self.assertNotIn("加宽", daily.label)
        # every other stage names its own structure point with its value
        for stage_name, fragment in (("broken", "当日与昨日真实低点的较低者"), ("breakout_hold", "突破平台10.30下方半个百分点"),
                                     ("trend_hold", "MA10 12.91下方半个百分点"), ("pullback_hold", "近5日收盘低点11.20"),
                                     ("base_platform", "10日最低收盘9.98"), ("unclassified", "当日与昨日真实低点的较低者")):
            with self.subTest(stage=stage_name):
                plan = generate(stage_inputs(stage_name))
                daily = next(line for line in plan.lines_of("hard_stop") if line.confirm.basis == "daily")
                self.assertIn(fragment, daily.label)
                binding = daily.derivation.inputs["binding_term"]
                self.assertEqual("加宽" in daily.label, binding != "structure")
                if binding == "atr":
                    self.assertIn("按 0.9×ATR14 向下加宽", daily.label)


class PlanKeyTests(unittest.TestCase):
    """Principle 8: a re-derivation on the same day must be storable, not a conflict."""

    def test_the_same_evidence_re_derives_the_same_key(self):
        self.assertEqual(generate(shenqi_inputs()).plan_key, generate(shenqi_inputs()).plan_key)

    def test_a_second_run_on_the_same_day_with_moved_evidence_gets_its_own_key(self):
        morning = generate(shenqi_inputs(as_of=datetime(2026, 9, 18, 10, 0, tzinfo=SH)))
        bars = shenqi_bars()
        bars[-1] = {**bars[-1], "close": 8.35, "low": 8.05}       # the forming bar moved
        afternoon = generate(shenqi_inputs(as_of=datetime(2026, 9, 18, 14, 0, tzinfo=SH), bars=bars))
        self.assertEqual(morning.trading_date, afternoon.trading_date)
        self.assertNotEqual(morning.plan_key, afternoon.plan_key)
        self.assertNotEqual(morning.inputs_hash, afternoon.inputs_hash)
        for plan in (morning, afternoon):
            head, _, digest = plan.plan_key.rpartition(":")
            self.assertEqual(head, "citics-primary:600613.SH:2026-09-18:holding")
            self.assertEqual(digest, plan.inputs_hash[:12])


class LineWordingTests(unittest.TestCase):
    """The sentence a human reads and the conditions a machine evaluates are one source."""

    def test_the_soft_stop_claims_an_industry_condition_only_when_it_carries_one(self):
        with_sector = generate(rally_inputs()).lines_of("soft_stop")[0]
        without = generate(rally_inputs(sector=None)).lines_of("soft_stop")[0]
        self.assertEqual(with_sector.extra, ["sector_change_negative"])
        self.assertIn("行业当日翻绿", with_sector.label)
        self.assertEqual(without.extra, [])
        self.assertNotIn("行业", without.label)
        self.assertIn("减半仓", without.label)

    def test_the_new_buy_trigger_names_exactly_the_conditions_it_evaluates(self):
        payload = {"position": None, "lane": {"lane": "reclaim", "reference": 8.60, "support": 8.05}}
        with_sector = generate(shenqi_inputs(**payload)).lines_of("trigger")[0]
        without = generate(shenqi_inputs(sector=None, **payload)).lines_of("trigger")[0]
        self.assertIn("sector_not_weak", with_sector.extra)
        self.assertIn("行业当日不走弱", with_sector.label)
        self.assertEqual(without.extra, ["amount_ge_prev_day"])
        self.assertNotIn("行业", without.label)
        self.assertIn("成交额不低于前一日", without.label)


class SharedMetricDefinitionTests(unittest.TestCase):
    """The lane report and the discipline card must never quote different numbers."""

    def test_the_volatility_buffer_has_one_definition(self):
        for volatility in (0.0, 1.5, 2.5, 4.0, 9.0):
            metrics = {"volatility": volatility}
            self.assertEqual(buffer_pct(metrics), volatility_buffer_pct(metrics))
            self.assertAlmostEqual(volatility_buffer_pct(metrics), max(0.015, volatility * 0.006), places=12)

    def test_stage_metrics_agree_with_the_lane_features_on_the_shared_keys(self):
        closes = STAGE_CLOSES["trend_hold"]
        bars = series_from_closes(closes)
        days = [row["trading_date"] for row in bars]
        lane_rows = [{"trade_date": row["trading_date"], "close": row["close"], "amount": row["amount"],
                      "main_net": 0.0,
                      "pct_chg": round((row["close"] / previous["close"] - 1) * 100, 2),
                      "turnover_rate": 1.0}
                     for previous, row in zip(bars, bars[1:])]
        lane = rules_features(lane_rows, days[1:])
        stage = daily_metrics(bars)
        for key in ("ma5", "ma10", "prior_high", "recent_low"):
            self.assertAlmostEqual(stage[key], lane[key], places=9, msg=key)


if __name__ == "__main__":
    unittest.main()
