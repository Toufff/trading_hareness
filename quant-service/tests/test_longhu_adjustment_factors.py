"""The longhu-derived cumulative adjustment factor: the math, the plan, the writer.

Fixtures are real production numbers (read-only, 2026-09-19): canonical raw
closes and published pre_close, the vendor's qfq close and CQ record from
``GetKLineDay_W14``, and the tushare cumulative factor the derivation must
reproduce.  tushare stores 3-4 decimals, so "reproduce" means within
``CHECKPOINT_AGREEMENT`` (0.2%), the same bound the validation mode uses.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

import app.adjustment_factor_maintenance as maintenance
from app import longhu_adjustment_factors as lh
from app.longhu_adjustment_factors import (
    CHECKPOINT_AGREEMENT,
    BarPoint,
    Checkpoint,
    LonghuDay,
    WindowInputs,
    build_plan,
    decide_step,
    derive_symbol,
    ex_reference_price,
    fill_bar_gaps,
    parse_corporate_action,
    parse_kline_payload,
    persist_factor_date,
    validate,
)


def _close_to(test: unittest.TestCase, value: float, expected: float, tolerance: float = CHECKPOINT_AGREEMENT):
    test.assertLessEqual(abs(value / expected - 1.0), tolerance, f"{value} vs {expected}")


def _day(value: str, qfq: float | None, cq: str | None = None) -> LonghuDay:
    return LonghuDay(date.fromisoformat(value), qfq, cq)


def _bar(value: str, close: float, pre_close: float | None = None) -> BarPoint:
    return BarPoint(date.fromisoformat(value), close, pre_close)


class EvidenceParsingTests(unittest.TestCase):
    def test_the_payload_maps_dates_to_the_qfq_close_and_the_cq_record(self):
        days = parse_kline_payload({
            "x": ["20260806", "20260807"],
            "y": [[41.0, 41.36, 41.5, 40.9], [41.2, 40.09, 41.3, 39.9]],
            "CQ": ["", "0,0,0,12.3852"],
        })
        self.assertEqual(days[date(2026, 8, 6)], LonghuDay(date(2026, 8, 6), 41.36, None))
        self.assertEqual(days[date(2026, 8, 7)].cq, "0,0,0,12.3852")

    def test_a_malformed_payload_yields_nothing_rather_than_a_guess(self):
        self.assertEqual(parse_kline_payload({"x": ["20260806"], "y": []}), {})

    def test_cq_records(self):
        self.assertIsNone(parse_corporate_action(""))
        self.assertIsNone(parse_corporate_action("0,0,0,0"))
        action = parse_corporate_action("4.5,0,0,3.5")
        self.assertEqual((action.shares_per10, action.cash_per10), (4.5, 3.5))
        self.assertFalse(action.has_rights_fields)
        self.assertTrue(parse_corporate_action("0,4,33.6,0").has_rights_fields)
        unparsed = parse_corporate_action("x")
        self.assertIsNotNone(unparsed, "a recorded marker is still an action marker")

    def test_the_cq_record_reproduces_the_published_ex_rights_price(self):
        # Published pre_close on the ex-date, production 2026-07.
        self.assertEqual(ex_reference_price(33.78, parse_corporate_action("4.8,0,0,5")), 22.49)
        self.assertEqual(ex_reference_price(44.73, parse_corporate_action("4.8,0,0,0")), 30.22)
        self.assertEqual(ex_reference_price(24.76, parse_corporate_action("4.5,0,0,3.5")), 16.83)


class StepRuleTests(unittest.TestCase):
    def test_689009_cash_dividend_where_the_published_price_beats_the_cq_reference(self):
        # CQ says 1.23852/share -> 41.36; the exchange published 41.38.
        decision = decide_step(
            _bar("2026-08-06", 42.6, 42.26), _bar("2026-08-07", 40.09, 41.38),
            _day("2026-08-06", 41.36), _day("2026-08-07", 40.09, "0,0,0,12.3852"))
        self.assertEqual(decision.basis, "pre_close_over_cq")
        self.assertIn("cq_reference_disagrees", decision.flags)
        _close_to(self, decision.step, 1.064 / 1.033)

    def test_605577_cash_dividend_agreeing_with_the_published_price(self):
        decision = decide_step(
            _bar("2026-07-02", 9.14, 9.01), _bar("2026-07-03", 9.19, 8.98),
            _day("2026-07-02", 8.98), _day("2026-07-03", 9.19, "0,0,0,1.6"))
        self.assertEqual(decision.basis, "cq_pre_close")
        _close_to(self, decision.step, 1.0542 / 1.0357)

    def test_an_ordinary_day_before_an_action_is_exactly_one_despite_the_qfq_drift(self):
        # 605577.SH 07-01 -> 07-02: qfq/raw 0.98224 -> 0.98249 (subtractive
        # adjustment drifts with the price); the step must still be 1 exactly.
        decision = decide_step(
            _bar("2026-07-01", 9.01, 8.81), _bar("2026-07-02", 9.14, 9.01),
            _day("2026-07-01", 8.85), _day("2026-07-02", 8.98))
        self.assertEqual((decision.step, decision.basis), (1.0, "none"))

    def test_600519_no_action_keeps_the_factor_bit_for_bit(self):
        bars = [_bar("2026-08-03", 1358.98, 1350.6), _bar("2026-08-04", 1328.36, 1358.98),
                _bar("2026-08-05", 1306.45, 1328.36), _bar("2026-08-06", 1308.55, 1306.45)]
        longhu = {bar.trading_date: LonghuDay(bar.trading_date, bar.close) for bar in bars}
        result = derive_symbol("600519.SH", bars, longhu, anchor_date=bars[0].trading_date,
                               anchor_factor=8.6463, anchor_provider="tushare_super_get")
        self.assertEqual([row.adj_factor for row in result.factors], [Decimal("8.6463")] * 3)
        self.assertTrue(all(row.provider == lh.PROVIDER_KEY for row in result.factors))

    def test_a_sub_two_yuan_dividend_is_sized_by_the_published_price(self):
        # 000709.SZ 2026-07-23: 0.04 cash on a 2.10 stock.
        decision = decide_step(
            _bar("2026-07-22", 2.10, 2.09), _bar("2026-07-23", 2.11, 2.06),
            _day("2026-07-22", 2.06), _day("2026-07-23", 2.11, "0,0,0,0.4"))
        self.assertEqual(decision.basis, "cq_pre_close")
        _close_to(self, decision.step, 11.7062 / 11.4832)

    def test_rounding_noise_on_a_sub_two_yuan_stock_is_never_an_action(self):
        # qfq rounded to 0.01 on a 1.5-yuan stock: the qfq step wobbles by
        # ~0.4% on an ordinary day.  No CQ, no pre_close move: exactly 1.
        decision = decide_step(
            _bar("2026-08-25", 1.53, 1.53), _bar("2026-08-26", 1.64, 1.53),
            _day("2026-08-25", 1.49), _day("2026-08-26", 1.61))
        self.assertEqual((decision.step, decision.basis), (1.0, "none"))

    def test_a_one_tick_pre_close_wobble_without_a_record_is_rejected(self):
        decision = decide_step(
            _bar("2026-09-08", 1.53), _bar("2026-09-09", 1.55, 1.52),
            _day("2026-09-08", 1.53), _day("2026-09-09", 1.55))
        self.assertEqual(decision.step, 1.0)
        self.assertIn("pre_close_signature_rejected_by_qfq", decision.flags)

    def test_a_falling_step_without_a_record_is_a_bad_close_not_an_action(self):
        # 600664.SH 2026-08-31: a legacy quote close of 9.00 against a real 9.12.
        decision = decide_step(
            _bar("2026-08-31", 9.0), _bar("2026-09-01", 9.49, 9.12),
            _day("2026-08-31", 9.12), _day("2026-09-01", 9.49))
        self.assertEqual(decision.step, 1.0)
        self.assertIn("decreasing_step_rejected", decision.flags)

    def test_a_missed_pre_close_signature_is_caught_by_the_record_and_the_qfq_series(self):
        # The vendor bar's pre_close did not move, but the CQ record says 0.50
        # cash and the qfq series shows exactly that step.
        decision = decide_step(
            _bar("2026-09-10", 20.00), _bar("2026-09-11", 20.30, 20.00),
            _day("2026-09-10", 19.50), _day("2026-09-11", 20.30, "0,0,0,5"))
        self.assertEqual(decision.basis, "cq_qfq")
        self.assertIn("pre_close_missed_action", decision.flags)
        _close_to(self, decision.step, 20.0 / 19.5, 1e-9)

    def test_a_sub_tick_record_with_no_price_move_is_not_an_action(self):
        # 300583.SZ 2026-07-14: 0.005/share recorded, exchange price unchanged,
        # tushare kept its factor.
        decision = decide_step(
            _bar("2026-07-13", 11.68), _bar("2026-07-14", 11.70, 11.68),
            _day("2026-07-13", 11.68), _day("2026-07-14", 11.70, "0,0,0,0.05"))
        self.assertEqual(decision.step, 1.0)
        self.assertIn("cq_without_price_move", decision.flags)

    def test_without_any_longhu_evidence_a_pre_close_move_is_still_used(self):
        decision = decide_step(
            _bar("2026-07-02", 9.14), _bar("2026-07-03", 9.19, 8.98), None, None,
            longhu_available=False)
        self.assertEqual(decision.basis, "pre_close_only")
        self.assertIn("longhu_missing", decision.flags)

    def test_a_pre_close_move_across_uncovered_sessions_is_unresolved(self):
        decision = decide_step(
            _bar("2026-09-11", 51.52), _bar("2026-09-15", 53.77, 50.10), None, None,
            longhu_available=False, sessions_between=1)
        self.assertIn("unresolved", decision.flags)


class ChainTests(unittest.TestCase):
    def test_multiple_actions_in_one_fetched_window_compound(self):
        bars = [_bar("2026-08-31", 18.00, 17.90), _bar("2026-09-01", 18.09, 17.96),
                _bar("2026-09-02", 18.20, 18.09), _bar("2026-09-03", 18.77, 18.14)]
        # qfq rebased on the latest bar: 0.04 then 0.06 cash subtracted.
        longhu = {
            date(2026, 8, 31): LonghuDay(date(2026, 8, 31), 17.90),
            date(2026, 9, 1): LonghuDay(date(2026, 9, 1), 18.03, "0,0,0,0.4"),
            date(2026, 9, 2): LonghuDay(date(2026, 9, 2), 18.14),
            date(2026, 9, 3): LonghuDay(date(2026, 9, 3), 18.77, "0,0,0,0.6"),
        }
        # 09-01 pre_close 17.96 = 18.00 - 0.04; 09-03 pre_close 18.14 = 18.20 - 0.06.
        result = derive_symbol("000681.SZ", bars, longhu, anchor_date=date(2026, 8, 31),
                               anchor_factor=5.0751, anchor_provider="tushare_primary")
        steps = [decision.step for decision in result.decisions]
        self.assertAlmostEqual(steps[0], 18.00 / 17.96, places=12)
        self.assertEqual(steps[1], 1.0)
        self.assertAlmostEqual(steps[2], 18.20 / 18.14, places=12)
        expected = 5.0751 * (18.00 / 17.96) * (18.20 / 18.14)
        self.assertEqual(result.factors[-1].adj_factor, lh.factor_value(expected))

    def test_a_suspension_gap_keeps_the_resumption_pre_close_as_evidence(self):
        # Suspended 07-15..07-17 (the vendor has no candles either); an ex-date
        # on the resumption day is still sized by its published pre_close.
        bars = [_bar("2026-07-14", 10.00), _bar("2026-07-20", 10.20, 9.70)]
        longhu = {
            date(2026, 7, 14): LonghuDay(date(2026, 7, 14), 9.70),
            date(2026, 7, 20): LonghuDay(date(2026, 7, 20), 10.20, "0,0,0,3"),
        }
        calendar = [date(2026, 7, 14), date(2026, 7, 15), date(2026, 7, 16), date(2026, 7, 17),
                    date(2026, 7, 20)]
        result = derive_symbol("600000.SH", bars, longhu, anchor_date=date(2026, 7, 14),
                               anchor_factor=2.0, calendar=calendar)
        self.assertEqual(result.truncated_dates, [])
        self.assertEqual(result.decisions[0].basis, "cq_pre_close")
        self.assertEqual(result.factors[0].adj_factor, lh.factor_value(2.0 * 10.0 / 9.7))

    def test_a_canonical_hole_the_vendor_traded_is_reconstructed_not_guessed(self):
        # 000672.SZ: no canonical 09-07 bar; the 09-08 pre_close (14.87) is the
        # 09-07 close ex a 0.157 dividend, NOT the 09-04 close.
        bars = [_bar("2026-09-04", 14.89, 14.62), _bar("2026-09-08", 15.18, 14.87)]
        longhu = {
            date(2026, 9, 4): LonghuDay(date(2026, 9, 4), 14.73),
            date(2026, 9, 7): LonghuDay(date(2026, 9, 7), 14.87),
            date(2026, 9, 8): LonghuDay(date(2026, 9, 8), 15.18, "0,0,0,1.57"),
        }
        filled = fill_bar_gaps(bars, longhu)
        self.assertEqual([bar.trading_date for bar in filled],
                         [date(2026, 9, 4), date(2026, 9, 7), date(2026, 9, 8)])
        self.assertTrue(filled[1].virtual)
        self.assertEqual(filled[1].close, 15.03)
        result = derive_symbol("000672.SZ", bars, longhu, anchor_date=date(2026, 9, 4),
                               anchor_factor=9.4828)
        self.assertEqual([row.trading_date for row in result.factors], [date(2026, 9, 8)],
                         "a reconstructed session is never written")
        _close_to(self, float(result.factors[0].adj_factor), 9.5784)

    def test_without_vendor_evidence_an_uncovered_gap_truncates_the_chain(self):
        bars = [_bar("2026-09-11", 51.52), _bar("2026-09-15", 53.77, 52.79),
                _bar("2026-09-16", 55.88, 53.77)]
        calendar = [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
        result = derive_symbol("002080.SZ", bars, {}, anchor_date=date(2026, 9, 11),
                               anchor_factor=5.536, calendar=calendar)
        self.assertEqual(result.factors, [])
        self.assertEqual(result.truncated_dates, [date(2026, 9, 15), date(2026, 9, 16)])

    def test_a_new_listing_without_an_anchor_starts_at_one(self):
        bars = [_bar("2026-09-10", 30.0, 12.0), _bar("2026-09-11", 31.0, 30.0)]
        longhu = {bar.trading_date: LonghuDay(bar.trading_date, bar.close) for bar in bars}
        result = derive_symbol("301699.SZ", bars, longhu, anchor_date=None, anchor_factor=None,
                               new_listing=True)
        self.assertEqual([row.adj_factor for row in result.factors], [Decimal("1.0"), Decimal("1.0")])
        self.assertEqual(result.anchor["kind"], "new_listing_starts_at_one")

    def test_no_lineage_and_a_missing_anchor_bar_are_held_with_a_reason(self):
        self.assertEqual(derive_symbol("000001.SH", [_bar("2026-09-10", 3000.0)], {},
                                       anchor_date=None, anchor_factor=None).held_reason,
                         "no_factor_lineage")
        self.assertEqual(derive_symbol("600000.SH", [_bar("2026-09-10", 10.0)], {},
                                       anchor_date=date(2026, 9, 9), anchor_factor=2.0).held_reason,
                         "anchor_bar_missing")

    def test_a_stored_factor_inside_the_window_wins_and_is_compared(self):
        bars = [_bar("2026-09-01", 9.0), _bar("2026-09-02", 9.2, 9.0), _bar("2026-09-03", 9.3, 9.2)]
        longhu = {bar.trading_date: LonghuDay(bar.trading_date, bar.close) for bar in bars}
        stored = {date(2026, 9, 2): Checkpoint(date(2026, 9, 2), 1.05, "tushare_super_sdk")}
        result = derive_symbol("600000.SH", bars, longhu, anchor_date=date(2026, 9, 1),
                               anchor_factor=1.0, checkpoints=stored)
        first, second = result.factors
        self.assertEqual((first.provider, first.adj_factor), ("tushare_super_sdk", Decimal("1.05")))
        self.assertFalse(result.checkpoints_compared[0]["agrees"])
        # ... and the chain continues from the stored value, never from the guess.
        self.assertEqual((second.provider, second.adj_factor), (lh.PROVIDER_KEY, Decimal("1.05")))


def _inputs(**overrides) -> WindowInputs:
    base = dict(
        from_date=date(2026, 9, 2), to_date=date(2026, 9, 3),
        symbols={"600000.SH": {"symbol": "600000.SH", "anchor_date": date(2026, 9, 1),
                               "anchor_factor": Decimal("2"), "anchor_provider": "tushare_primary",
                               "first_bar_date": date(2023, 1, 3)},
                 "000001.SH": {"symbol": "000001.SH", "anchor_date": None, "anchor_factor": None,
                               "anchor_provider": None, "first_bar_date": date(2023, 1, 3)}},
        bars={"600000.SH": [_bar("2026-09-01", 10.0), _bar("2026-09-02", 10.1, 10.0),
                            _bar("2026-09-03", 10.2, 10.1)],
              "000001.SH": [_bar("2026-09-02", 3000.0), _bar("2026-09-03", 3001.0)]},
        current_bar_factor={("600000.SH", date(2026, 9, 2)): Decimal("1"),
                            ("600000.SH", date(2026, 9, 3)): None},
        stored={}, market_bar_factor={("600000.SH", date(2026, 9, 2)): None},
        sessions=[date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)],
    )
    base.update(overrides)
    return WindowInputs(**base)


class PlanTests(unittest.TestCase):
    def test_the_plan_writes_every_target_date_and_holds_what_it_cannot_derive(self):
        longhu = {"600000.SH": {bar.trading_date: LonghuDay(bar.trading_date, bar.close)
                                for bar in _inputs().bars["600000.SH"]}}
        plan = build_plan(_inputs(), longhu, write_dates=[date(2026, 9, 2), date(2026, 9, 3)])
        self.assertEqual([row.symbol for row in plan.rows[date(2026, 9, 2)]], ["600000.SH"])
        self.assertEqual(plan.held, {"000001.SH": "no_factor_lineage"})
        self.assertEqual(plan.clear[date(2026, 9, 2)], ["000001.SH"])
        summary = lh.plan_summary(plan)
        self.assertEqual(summary["dates"][0]["replaces_other_value"], 1, "the placeholder 1")
        self.assertEqual(summary["dates"][1]["fills_null"], 1)
        self.assertEqual(summary["anchors_missing"], ["000001.SH"])

    def test_null_only_dates_fill_holes_and_leave_real_values_alone(self):
        longhu = {"600000.SH": {bar.trading_date: LonghuDay(bar.trading_date, bar.close)
                                for bar in _inputs().bars["600000.SH"]}}
        plan = build_plan(_inputs(), longhu, write_dates=[],
                          null_only_dates=[date(2026, 9, 2), date(2026, 9, 3)])
        self.assertEqual(plan.rows[date(2026, 9, 2)], [], "the bar already carries a value")
        self.assertEqual([row.symbol for row in plan.rows[date(2026, 9, 3)]], ["600000.SH"])

    def test_the_nightly_lane_keeps_its_earlier_derivation_on_dates_it_is_not_reworking(self):
        stored = {"600000.SH": {date(2026, 9, 2): Checkpoint(
            date(2026, 9, 2), 2.5, lh.PROVIDER_KEY, "corporate_action_cumulative")}}
        longhu = {"600000.SH": {bar.trading_date: LonghuDay(bar.trading_date, bar.close)
                                for bar in _inputs().bars["600000.SH"]}}
        nightly = build_plan(_inputs(stored=stored), longhu, write_dates=[date(2026, 9, 3)],
                             rederive_derived=False)
        self.assertEqual(nightly.rows[date(2026, 9, 3)][0].adj_factor, Decimal("2.5"))
        repair = build_plan(_inputs(stored=stored), longhu, write_dates=[date(2026, 9, 2), date(2026, 9, 3)],
                            rederive_derived=True)
        self.assertEqual(repair.rows[date(2026, 9, 3)][0].adj_factor, Decimal("2.0"),
                         "the repair recomputes its own rows from the anchor (idempotent)")


class _RecordingConnection:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(str(sql).split()), params))

        class Result:
            rowcount = 1
        return Result()


class WriterTests(unittest.TestCase):
    def _row(self, symbol="600000.SH", provider=lh.PROVIDER_KEY,
             semantics="corporate_action_cumulative", value="2.1"):
        return lh.DerivedFactor(symbol, date(2026, 9, 2), Decimal(value), provider, semantics, None,
                                {"date": "2026-09-01", "adj_factor": 2.0})

    def test_one_date_is_written_to_the_evidence_and_both_bar_tables(self):
        connection = _RecordingConnection()
        counts = persist_factor_date(
            connection, date(2026, 9, 2), [self._row(), self._row("600001.SH", "tushare_primary", "", "3")],
            available_at=datetime(2026, 9, 19, tzinfo=timezone.utc), clear_symbols=["000001.SH"])
        statements = [sql for sql, _ in connection.calls]
        self.assertTrue(statements[0].startswith("INSERT INTO quant.daily_adjustment_factors"))
        upsert_params = connection.calls[0][1]
        self.assertEqual(upsert_params["symbols"], ["600000.SH"], "stored tushare rows are not re-inserted")
        self.assertIn('"factor_semantics": "corporate_action_cumulative"', upsert_params["raws"][0])
        self.assertIn('"source": "longhuvip:GetKLineDay_W14"', upsert_params["raws"][0])
        self.assertTrue(any("UPDATE quant.canonical_bars_daily bar SET adj_factor=t.adj_factor" in sql
                            for sql in statements))
        self.assertTrue(any("UPDATE quant.market_bars_daily bar SET adj_factor=t.adj_factor" in sql
                            for sql in statements))
        cleared = next(params for sql, params in connection.calls if "SET adj_factor=NULL" in sql)
        self.assertEqual(cleared[1], ["000001.SH"])
        self.assertTrue(any("'superseded_at'" in sql for sql in statements), "placeholders annotated")
        self.assertEqual(counts["refused"], 0)

    def test_a_derived_row_without_explicit_cumulative_semantics_is_refused(self):
        connection = _RecordingConnection()
        counts = persist_factor_date(connection, date(2026, 9, 2), [self._row(semantics="")],
                                     available_at=datetime(2026, 9, 19, tzinfo=timezone.utc))
        self.assertEqual(counts["refused"], 1)
        self.assertFalse(any("SET adj_factor=t.adj_factor" in sql for sql, _ in connection.calls))

    def test_a_symbol_with_a_planned_row_is_never_cleared(self):
        connection = _RecordingConnection()
        persist_factor_date(connection, date(2026, 9, 2), [self._row()],
                            available_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
                            clear_symbols=["600000.SH"])
        self.assertFalse(any("SET adj_factor=NULL" in sql for sql, _ in connection.calls))


class ValidationTests(unittest.TestCase):
    def test_the_score_counts_matches_misses_and_false_actions(self):
        bars = {"605577.SH": [_bar("2026-07-01", 9.01, 8.81), _bar("2026-07-02", 9.14, 9.01),
                              _bar("2026-07-03", 9.19, 8.98), _bar("2026-07-06", 9.29, 9.19)]}
        truth = {"605577.SH": {date(2026, 7, 1): 1.0357, date(2026, 7, 2): 1.0357,
                               date(2026, 7, 3): 1.0542, date(2026, 7, 6): 1.0542}}
        longhu = {"605577.SH": {
            date(2026, 7, 1): LonghuDay(date(2026, 7, 1), 8.85),
            date(2026, 7, 2): LonghuDay(date(2026, 7, 2), 8.98),
            date(2026, 7, 3): LonghuDay(date(2026, 7, 3), 9.19, "0,0,0,1.6"),
            date(2026, 7, 6): LonghuDay(date(2026, 7, 6), 9.29)}}
        report = validate(bars, truth, longhu)
        self.assertEqual((report["step_pairs"], report["true_actions"], report["matched_actions"],
                          report["missed_actions"], report["false_actions"]), (3, 1, 1, 0, 0))
        self.assertLess(report["chained_error_max"], CHECKPOINT_AGREEMENT)


class _Deps:
    """Just enough of the dependency bundle for the per-date unit."""

    def __init__(self, source=None, fetch=None):
        self.database = object()
        self.source = source
        self.fetch = fetch

    async def run_database(self, action, *args, **_kwargs):
        return action(*args)

    def longhu_source(self):
        return self.source

    async def run_public(self, action, *args, **kwargs):
        kwargs.pop("timeout_seconds", None)
        return (self.fetch or action)(*args, **kwargs)

    @staticmethod
    def safe_error_detail(value, limit):
        return str(value)[:limit]

    @staticmethod
    def now():
        return datetime(2026, 9, 19, tzinfo=timezone.utc)


class LaneUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.originals = {name: getattr(maintenance, name)
                          for name in ("_read_window", "daily_row_count", "_persist_date")}
        self.written: list[tuple] = []
        maintenance._read_window = lambda _db, _start, _end: _inputs()
        maintenance.daily_row_count = lambda _db, _date: 1
        maintenance._persist_date = lambda _db, value, rows, clear, _at: (
            self.written.append((value, [row.symbol for row in rows], clear)) or {"canonical_updates": len(rows)})

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(maintenance, name, value)

    async def test_a_date_is_derived_from_longhu_and_written_in_one_step(self):
        def fetch(_source, sessions, **_kwargs):
            return ({"600000.SH": {bar.trading_date: LonghuDay(bar.trading_date, bar.close)
                                   for bar in _inputs().bars["600000.SH"]}}, {})

        session = maintenance.FactorLaneSession(_Deps(fetch=fetch), [date(2026, 9, 2)], date(2026, 9, 19))
        outcome = await maintenance.repair_factor_date(session, date(2026, 9, 2))
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["provider"], "longhu_qfq_derived")
        self.assertEqual(self.written, [(date(2026, 9, 2), ["600000.SH"], ["000001.SH"])])

    async def test_a_date_without_a_settled_cross_section_is_a_coverage_skip(self):
        maintenance.daily_row_count = lambda _db, _date: 0
        session = maintenance.FactorLaneSession(_Deps(), [date(2026, 9, 2)], date(2026, 9, 19))
        outcome = await maintenance.repair_factor_date(session, date(2026, 9, 2))
        self.assertEqual((outcome["status"], outcome["blocked_by"]), ("blocked", "coverage"))
        self.assertEqual(maintenance._classify(outcome), "skipped")

    async def test_a_longhu_outage_is_a_provider_failure_and_writes_nothing(self):
        def fetch(_source, sessions, **_kwargs):
            return {}, {symbol: "UpstreamStockApiError: HTTP 503" for symbol in sessions}

        session = maintenance.FactorLaneSession(_Deps(fetch=fetch), [date(2026, 9, 2)], date(2026, 9, 19))
        outcome = await maintenance.repair_factor_date(session, date(2026, 9, 2))
        self.assertEqual(outcome["blocked_by"], "provider")
        self.assertEqual(maintenance._classify(outcome), "failed")
        self.assertEqual(self.written, [])


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.originals = {name: getattr(maintenance, name) for name in (
            "_repair_window", "_read_window", "_repair_projection", "_apply_repair_date",
            "_repair_readback")}
        self.applied: list[date] = []
        maintenance._repair_window = lambda _db, _today, _from, _to, _sessions: {
            "from_date": date(2026, 9, 2), "to_date": date(2026, 9, 3), "derived_from_date": date(2026, 9, 2),
            "latest_settled_date": date(2026, 9, 3), "damaged_dates": [],
            "sessions": [date(2026, 9, 2), date(2026, 9, 3)]}
        maintenance._read_window = lambda _db, _start, _end: _inputs()
        maintenance._repair_projection = lambda _db, _plan: {"guard_after_apply_projected": {}}
        maintenance._apply_repair_date = lambda _db, value, rows, clear, _at: (
            self.applied.append(value) or {"canonical_updates": len(rows)})
        maintenance._repair_readback = lambda _db, _start, _end: {
            "guard": {"canonical_bars_daily": 0, "market_bars_daily": 0}, "dates": []}

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(maintenance, name, value)

    @staticmethod
    def _fetch(_source, sessions, **_kwargs):
        return ({"600000.SH": {bar.trading_date: LonghuDay(bar.trading_date, bar.close)
                               for bar in _inputs().bars["600000.SH"]}}, {})

    async def test_the_dry_run_plans_and_never_applies(self):
        report = await maintenance.repair(_Deps(fetch=self._fetch), apply=False, today=date(2026, 9, 19))
        self.assertEqual(report["status"], "planned")
        self.assertEqual(self.applied, [])
        self.assertEqual(report["plan"]["write_dates"], ["2026-09-02", "2026-09-03"])

    async def test_apply_writes_one_transaction_per_date_in_order_and_checks_the_guard(self):
        report = await maintenance.repair(_Deps(fetch=self._fetch), apply=True, today=date(2026, 9, 19))
        self.assertEqual(self.applied, [date(2026, 9, 2), date(2026, 9, 3)])
        self.assertEqual(report["status"], "completed")

    async def test_a_guard_above_zero_after_apply_fails_the_repair(self):
        maintenance._repair_readback = lambda _db, _start, _end: {
            "guard": {"canonical_bars_daily": 3, "market_bars_daily": 0}, "dates": []}
        report = await maintenance.repair(_Deps(fetch=self._fetch), apply=True, today=date(2026, 9, 19))
        self.assertEqual(report["status"], "failed")


class NoTushareOnTheFactorLaneTests(unittest.TestCase):
    """The user decision, pinned: the factor lane never calls tushare."""

    def test_the_lane_modules_import_and_call_no_tushare_client(self):
        from pathlib import Path

        app_dir = Path(__file__).resolve().parents[1] / "app"
        for name in ("adjustment_factor_maintenance.py", "longhu_adjustment_factors.py"):
            text = (app_dir / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                for forbidden in ("call_tushare_api", "tushare_providers", "call_with_fallback",
                                  "full_market_daily_controls_sync import COVERAGE_BLOCK_REASON, sync",
                                  "sync_daily_controls"):
                    self.assertNotIn(forbidden, text)

    def test_the_composition_root_hands_the_lane_no_tushare_dependency(self):
        import dataclasses

        fields = {field.name for field in dataclasses.fields(maintenance.AdjustmentFactorMaintenanceDependencies)}
        self.assertFalse({name for name in fields if "tushare" in name})
        self.assertIn("longhu_source", fields)


if __name__ == "__main__":
    unittest.main()
