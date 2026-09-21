"""Data-derived exposure caps: the calibration method, the committed artifact and its use in sizing."""

from __future__ import annotations

import hashlib
import json
import random
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.trade_discipline import exposure_calibration as calibration
from app.trade_discipline.generator import generate
from app.trade_discipline.stage import STAGES, classify_stage, daily_metrics
from app.trade_discipline.templates import build_sizing, exposure_text

from test_trade_discipline_core import calibrated, shenqi_inputs, stage_inputs, synthetic_calibration

ROOT = Path(__file__).resolve().parents[2]


def weekdays(start: date, count: int) -> list[date]:
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def series(symbol: str = "600000.SH", count: int = 80, *, factor: float = 1.0, start=date(2025, 1, 6)) -> list[dict]:
    rows, close = [], 10.0
    for index, day in enumerate(weekdays(start, count)):
        close = round(close * (1.004 if index % 3 else 0.992), 2)
        rows.append({"symbol": symbol, "trading_date": day, "open": close, "high": round(close * 1.02, 2),
                     "low": round(close * 0.98, 2), "close": close, "pre_close": close, "volume": 1e6 + index,
                     "amount": close * 1e6, "adj_factor": factor, "is_suspended": False,
                     "limit_down": round(close * 0.9, 2)})
    return rows


class PurePieceTests(unittest.TestCase):
    def test_quantile_is_numpy_linear_interpolation(self):
        values = [float(value) for value in range(1, 101)]
        self.assertAlmostEqual(calibration.quantile(values, 99), 99.01)
        self.assertAlmostEqual(calibration.quantile(values, 50), 50.5)
        self.assertEqual(calibration.quantile([3.0], 99), 3.0)
        shuffled = values[:]
        random.Random(7).shuffle(shuffled)
        self.assertEqual(calibration.quantile(shuffled, 99), calibration.quantile(values, 99))

    def test_the_cap_is_five_over_q99_rounded_down_to_five_and_clamped(self):
        self.assertEqual(calibration.cap_from_q99(13.413), 35)   # 5 / 13.413% = 37.3% -> 35
        self.assertEqual(calibration.cap_from_q99(10.0), 50)     # exactly 50
        self.assertEqual(calibration.cap_from_q99(25.458), 15)   # 19.6% -> 15
        self.assertEqual(calibration.cap_from_q99(60.0), 5)      # 8.3% -> 5 (floor of the clamp)
        self.assertEqual(calibration.cap_from_q99(200.0), 5)
        self.assertEqual(calibration.cap_from_q99(1.0), 50)      # 500% -> clamp 50
        self.assertEqual(calibration.cap_from_q99(60.0, minimum=0), 5)
        self.assertEqual(calibration.cap_from_q99(150.0, minimum=0), 0)  # holiday cells may go to 0
        self.assertEqual(calibration.cap_from_q99(-1.0), 50)

    def test_board_follows_the_platform_limit_helper_with_a_point_in_time_st_flag(self):
        self.assertEqual(calibration.board_key("600613.SH", False), "main_10")
        self.assertEqual(calibration.board_key("600613.SH", True), "st_5")
        self.assertEqual(calibration.board_key("300750.SZ", True), "growth_20")   # ChiNext ST keeps 20%
        self.assertEqual(calibration.board_key("688001.SH", False), "growth_20")
        self.assertEqual(calibration.board_key("920001.BJ", False), "bj_30")
        self.assertTrue(calibration.st_from_limit_band("600001.SH", 2.00, 1.90))
        self.assertFalse(calibration.st_from_limit_band("600001.SH", 8.54, 7.69))
        self.assertIsNone(calibration.st_from_limit_band("600001.SH", 8.54, None))
        self.assertIsNone(calibration.st_from_limit_band("600001.SH", 8.54, 6.00))


class SampleTests(unittest.TestCase):
    def test_the_loss_is_the_worst_low_of_the_next_two_sessions_against_the_close(self):
        rows = series()
        samples, counts = calibration.symbol_samples("600000.SH", rows)
        self.assertGreater(counts["samples"], 0)
        # the last usable sample is t = rows[-3]
        t, first, second = rows[-3], rows[-2], rows[-1]
        expected = 1 - min(first["low"], second["low"]) / t["close"]
        self.assertAlmostEqual(samples[-1][2], expected, places=12)
        self.assertEqual(counts["no_next_sessions"], 2)

    def test_the_stage_is_the_generators_own_classification_of_the_same_window(self):
        rows = series(count=90)
        samples, _ = calibration.symbol_samples("600000.SH", rows)
        t_index = len(rows) - 3
        window = [calibration.generator_bar(row) for row in rows[t_index - 59:t_index + 1]]
        self.assertEqual(samples[-1][0], classify_stage(daily_metrics(window), None)["stage"])

    def test_an_ex_rights_day_is_not_a_crash_on_adjusted_prices(self):
        rows = series(count=70)
        # 10-for-10 bonus on the last two sessions: raw price halves, the cumulative factor doubles
        for row in rows[-2:]:
            for key in ("open", "high", "low", "close", "pre_close", "limit_down"):
                row[key] = round(row[key] / 2, 3)
            row["adj_factor"] = 2.0
        samples, _ = calibration.symbol_samples("600000.SH", rows)
        self.assertLess(samples[-1][2], 0.05)              # raw prices would say ~50%

    def test_a_missing_factor_drops_the_sample_instead_of_using_raw_prices(self):
        rows = series(count=70)
        rows[-1]["adj_factor"] = None
        samples, counts = calibration.symbol_samples("600000.SH", rows)
        self.assertEqual(counts["factor_missing"], 1)      # the only t whose horizon reaches the unfactored bar
        self.assertEqual(len(samples), counts["samples"])

    def test_suspended_rows_are_not_sessions_and_long_gaps_drop_the_sample(self):
        rows = series(count=70)
        rows[-2]["is_suspended"] = True                    # t+1 is the next *traded* session
        samples, counts = calibration.symbol_samples("600000.SH", rows)
        t, first, second = rows[-4], rows[-3], rows[-1]
        self.assertAlmostEqual(samples[-1][2], 1 - min(first["low"], second["low"]) / t["close"], places=12)
        gapped = series(count=70)
        for row in gapped[-2:]:
            row["trading_date"] = row["trading_date"] + timedelta(days=30)
        _, gap_counts = calibration.symbol_samples("600000.SH", gapped)
        self.assertEqual(gap_counts["gap_too_long"], 2)

    def test_holiday_samples_are_the_sessions_reopening_after_a_long_closure(self):
        rows = series(count=70)
        resume = rows[-2]["trading_date"]
        samples, _ = calibration.symbol_samples("600000.SH", rows, holiday_resume_dates=frozenset({resume}))
        self.assertTrue(samples[-1][3])
        self.assertEqual(sum(1 for sample in samples if sample[3]), 1)
        sessions = [date(2026, 9, 30), date(2026, 10, 8), date(2026, 10, 9), date(2026, 9, 24), date(2026, 9, 28)]
        self.assertEqual(calibration.holiday_resume_dates(sessions), frozenset({date(2026, 10, 8)}))

    def test_a_st_band_selects_the_st_board(self):
        rows = series(count=70)
        for row in rows:
            row["limit_down"] = round(row["pre_close"] * 0.95, 2)
        samples, _ = calibration.symbol_samples("600000.SH", rows)
        self.assertEqual({sample[1] for sample in samples}, {"st_5"})


class CellTests(unittest.TestCase):
    def test_thin_cells_fall_back_to_the_board_pooled_across_stages(self):
        losses = {("broken", "main_10"): [0.01 * (index % 20) for index in range(400)],
                  ("trend_hold", "main_10"): [0.02] * 10}
        cells = calibration.build_cells(losses, ("broken", "trend_hold"), ("main_10",))
        self.assertIsNone(cells["broken|main_10"]["fallback"])
        self.assertEqual(cells["trend_hold|main_10"]["fallback"], "board_pooled_across_stages")
        self.assertEqual(cells["trend_hold|main_10"]["own_samples"], 10)
        self.assertEqual(cells["trend_hold|main_10"]["samples"], 410)
        self.assertEqual(cells["*|main_10"]["samples"], 410)

    def test_the_artifact_version_changes_with_its_cells(self):
        base = calibration.artifact(stage_cells={"a": {"cap_pct": 5}}, holiday_cells={}, first_date="x",
                                    last_date="y", diagnostics={})
        other = calibration.artifact(stage_cells={"a": {"cap_pct": 10}}, holiday_cells={}, first_date="x",
                                     last_date="y", diagnostics={})
        self.assertNotEqual(base["version"], other["version"])
        self.assertTrue(base["version"].startswith(calibration.CALIBRATION_METHOD_VERSION + ":"))


class CommittedArtifactTests(unittest.TestCase):
    """The artifact in the repository is complete, self-consistent and what the generator reads."""

    def setUp(self):
        self.artifact = json.loads(calibration.ARTIFACT_PATH.read_text(encoding="utf-8"))

    def test_it_declares_its_method_and_window(self):
        artifact = self.artifact
        self.assertEqual(artifact["tolerance_pct"], 5.0)
        self.assertEqual(artifact["percentile"], 99.0)
        self.assertEqual(artifact["horizon_sessions"], 2)
        self.assertEqual(artifact["min_samples"], 300)
        self.assertIn("5%", artifact["method"])
        self.assertLess(artifact["data_window"]["first_date"], artifact["data_window"]["last_date"])
        body = {"stage_cells": artifact["stage_cells"], "holiday_cells": artifact["holiday_cells"]}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        self.assertEqual(artifact["version"], f"{calibration.CALIBRATION_METHOD_VERSION}:{digest}")

    def test_every_stage_and_board_has_a_cell_whose_cap_follows_the_rule(self):
        for kind, minimum in (("stage_cells", 5), ("holiday_cells", 0)):
            cells = self.artifact[kind]
            for stage in STAGES:
                for board in calibration.BOARD_LABEL:
                    with self.subTest(kind=kind, cell=f"{stage}|{board}"):
                        cell = cells[f"{stage}|{board}"]
                        self.assertEqual(cell["cap_pct"], calibration.cap_from_q99(cell["q99_loss_pct"], minimum=minimum))
                        self.assertGreaterEqual(cell["samples"], calibration.MIN_SAMPLES)
                        self.assertEqual(cell["fallback"] is None, cell["source_cell"] == f"{stage}|{board}")
                        self.assertLessEqual(cell["q95_loss_pct"], cell["q99_loss_pct"])
        # broken is no longer forced flat
        self.assertGreater(self.artifact["stage_cells"]["broken|main_10"]["cap_pct"], 0)

    def test_the_hand_picked_table_is_gone(self):
        source = (ROOT / "quant-service/app/trade_discipline/templates.py").read_text(encoding="utf-8")
        self.assertNotIn("TARGET_EXPOSURE_PCT", source)
        self.assertNotIn("HOLIDAY_EXPOSURE_PCT", source)

    def test_the_calibration_script_only_reads(self):
        source = (ROOT / "scripts/calibrate-discipline-exposure.py").read_text(encoding="utf-8")
        self.assertIn("default_transaction_read_only=on", source)
        for verb in ("INSERT", "UPDATE ", "DELETE", "TRUNCATE", "ALTER ", "CREATE "):
            self.assertNotIn(verb, source)


class SizingWithCapsTests(unittest.TestCase):
    def test_calibrated_tail_reference_is_disclosed_but_does_not_force_a_reduction(self):
        plan = generate(shenqi_inputs())        # 600613.SH main board, crash_rebound, 5800 shares
        sizing = plan.sizing
        basis = sizing.exposure_basis
        cell = calibration.load_calibration()["stage_cells"]["crash_rebound|main_10"]
        self.assertEqual(basis["cell"], "crash_rebound|main_10")
        self.assertEqual(sizing.target_exposure_pct, Decimal(str(cell["cap_pct"])))
        self.assertEqual(basis["q99_loss_pct"], cell["q99_loss_pct"])
        self.assertEqual(basis["samples"], cell["samples"])
        self.assertEqual(sizing.recommended_shares, sizing.max_shares)
        self.assertEqual(sizing.binding_constraint, "risk")
        self.assertEqual(sizing.concentration_policy, "tail_risk_advisory")
        self.assertEqual(plan.lines_of("exposure"), [])
        self.assertGreater(sizing.tail_risk_estimated_loss_pct, 0)
        self.assertEqual(plan.metrics["exposure_calibration"]["stage"]["cell"], "crash_rebound|main_10")
        self.assertEqual(plan.status, "active")

    def test_no_exposure_line_when_the_position_is_within_the_recommendation(self):
        plan = generate(shenqi_inputs(position={**shenqi_inputs().position, "quantity": 500, "sellable_quantity": 500}))
        self.assertLessEqual(plan.sizing.current_shares, plan.sizing.recommended_shares)
        self.assertEqual(plan.lines_of("exposure"), [])
        self.assertTrue({check.check_id: check for check in plan.quality}["exposure_line_when_over_risk"].passed)

    def test_broken_is_sized_like_any_other_stage(self):
        plan = generate(stage_inputs("broken"))
        self.assertEqual(plan.stage, "broken")
        self.assertGreater(plan.sizing.target_exposure_pct, 0)
        self.assertGreater(plan.sizing.recommended_shares, 0)
        self.assertEqual(plan.status, "active")

    def test_the_tail_reference_text_explicitly_says_it_is_not_actionable(self):
        sizing = build_sizing(stage="broken", equity=Decimal("99632"), risk_per_trade_pct=Decimal("1.0"),
                              reference_price=Decimal("8.41"), hard_stop=Decimal("7.75"), current_shares=5800,
                              cap_pct=5, exposure_basis={"q99_loss_pct": 60.0, "board_label": "主板（10%）",
                                                         "tolerance_pct": 5.0, "percentile": 99.0})
        self.assertEqual(sizing.binding_constraint, "risk")
        self.assertEqual(exposure_text(sizing), "风险上限 1500 股（1.0%÷止损距离）；集中仓位压力参考 500 股（极端亏损5%÷该阶段"
                                                "主板（10%）99%两日最大跌幅60.00%=5%，仅提示、不触发减仓）；可执行建议上限 1500 股")

    def test_only_a_real_stop_risk_budget_breach_creates_an_exposure_line(self):
        plan = generate(shenqi_inputs(risk_per_trade_pct=Decimal("1.0")))
        sizing = plan.sizing
        self.assertGreater(sizing.current_shares, sizing.max_shares)
        exposure = plan.lines_of("exposure")[0]
        self.assertEqual(exposure.action.value, sizing.max_shares)
        self.assertIn("止损风险", exposure.label)
        self.assertIn("仅提示、不触发减仓", exposure.label)

    def test_the_calibration_version_is_evidence(self):
        inputs = shenqi_inputs()
        self.assertEqual(inputs.exposure_calibration_version, calibration.load_calibration()["version"])
        other = inputs.model_copy(update={"exposure_calibration_version": "discipline-exposure-calibration-v1:000000000000"})
        self.assertNotEqual(inputs.fingerprint(), other.fingerprint())
        with self.assertRaises(ValueError):
            generate(other)                     # plans are never derived from an artifact they do not name
        with calibrated(synthetic_calibration(stage_cap=30, holiday_cap=10)):
            synthetic = generate(shenqi_inputs())
        self.assertEqual(synthetic.sizing.target_exposure_pct, Decimal("30"))


if __name__ == "__main__":
    unittest.main()
