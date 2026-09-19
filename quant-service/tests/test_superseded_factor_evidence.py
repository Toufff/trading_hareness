"""A stored factor row marked ``superseded_at`` is evidence only, never a factor.

2026-09-19, 300176.SZ: tushare missed the 2026-08-21 rights issue (CQ
``0,4,33.6,0``; suspended 08-13..08-20; close 5.23 on 08-12, exchange
pre_close 4.72 on 08-21, step 1.108051) and kept publishing 4.5917 until 09-08;
it publishes 5.0878 itself from 09-09.  The operator re-anchored the bars to
5.0878, wrote ``longhu_qfq_derived`` rows with
``method=manual_reanchor_rights_issue`` and annotated the 14 wrong tushare rows
with ``superseded_at``/``superseded_by``/``superseded_reason``.

Pinned here: a superseded row is neither a checkpoint, an anchor, a "real
factor" for coverage, nor promotable evidence for the guards; an operator's
manual row is a checkpoint the repair never recomputes; rows without the marker
behave exactly as before.
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from decimal import Decimal

from app import longhu_adjustment_factors as lh
from app.adjustment_factor_maintenance import (
    HOLE_DATES_SQL,
    PENDING_DATES_SQL,
    REAL_FACTOR_PREDICATE_SQL,
    REPAIR_DAMAGED_DATES_SQL,
    STATUS_DATES_SQL,
    WINDOW_LEAKS_SQL,
    factor_value_mismatch_sql,
    identity_factor_leak_sql,
)
from app.longhu_adjustment_factors import BarPoint, Checkpoint, WindowInputs, build_plan, plan_summary
from app.stock_study_readiness_repository import ADJUSTMENT_WINDOW_SQL
from app.tushare_normalization import (
    SUPERSEDED_MARKER,
    promotable_adjustment_factor,
    promotable_factor_evidence_sql,
    promotable_factor_evidence_sql_param,
)

GUARD = f"->>'{SUPERSEDED_MARKER}' IS NULL"


class SupersededPredicateTests(unittest.TestCase):
    def test_the_one_predicate_refuses_a_superseded_row(self):
        for sql in (promotable_factor_evidence_sql("factor", "raw"),
                    promotable_factor_evidence_sql_param("factor", "raw"), REAL_FACTOR_PREDICATE_SQL):
            self.assertIn(f"factor.raw{GUARD}", sql)
        # The guard binds to the WHOLE predicate, not only its derived half.
        sql = promotable_factor_evidence_sql("f", "raw")
        self.assertTrue(sql.startswith("((("), sql)
        self.assertTrue(sql.endswith(f"AND f.raw{GUARD})"), sql)

    def test_every_consumer_of_stored_evidence_carries_the_guard(self):
        consumers = {
            "checkpoints": lh.CHECKPOINTS_SQL, "anchors": lh.WINDOW_SYMBOLS_SQL,
            "validation_truth": lh.VALIDATION_TRUTH_SQL,
            "leak_guard": identity_factor_leak_sql("canonical_bars_daily"),
            "value_mismatch": factor_value_mismatch_sql("canonical_bars_daily"),
            "work_list": PENDING_DATES_SQL, "status_coverage": STATUS_DATES_SQL,
            "hole_dates": HOLE_DATES_SQL, "window_leaks": WINDOW_LEAKS_SQL,
            "damaged_dates": REPAIR_DAMAGED_DATES_SQL, "readiness": ADJUSTMENT_WINDOW_SQL,
        }
        for name, sql in consumers.items():
            with self.subTest(name):
                self.assertIn(GUARD, sql)

    def test_the_python_twin_refuses_a_superseded_row_and_nothing_else_changes(self):
        self.assertTrue(promotable_adjustment_factor({}, provider_key="tushare_primary"))
        self.assertFalse(promotable_adjustment_factor(
            {SUPERSEDED_MARKER: "2026-09-19 15:16:41+08"}, provider_key="tushare_primary"))
        cumulative = {"factor_semantics": "corporate_action_cumulative"}
        self.assertTrue(promotable_adjustment_factor(cumulative, provider_key=lh.PROVIDER_KEY))
        self.assertFalse(promotable_adjustment_factor(
            {**cumulative, SUPERSEDED_MARKER: "x"}, provider_key=lh.PROVIDER_KEY))


def _manual_inputs(method: str) -> WindowInputs:
    """300176.SZ in miniature: anchor 4.5917, a 1.108051 rights step, re-anchored to 5.0878."""
    symbol = "300176.SZ"
    bars = [BarPoint(date(2026, 8, 12), 5.23, 5.20), BarPoint(date(2026, 8, 21), 4.80, 4.72),
            BarPoint(date(2026, 8, 24), 4.85, 4.80)]
    stored = {value: Checkpoint(value, 5.0878, lh.PROVIDER_KEY, "corporate_action_cumulative",
                                Decimal("5.0878"), method)
              for value in (date(2026, 8, 21), date(2026, 8, 24))}
    return WindowInputs(
        from_date=date(2026, 8, 21), to_date=date(2026, 8, 24),
        symbols={symbol: {"symbol": symbol, "anchor_date": date(2026, 8, 12),
                          "anchor_factor": Decimal("4.5917"), "anchor_provider": "tushare_primary",
                          "first_bar_date": date(2023, 8, 15)}},
        bars={symbol: bars},
        current_bar_factor={(symbol, date(2026, 8, 21)): Decimal("5.0878"),
                            (symbol, date(2026, 8, 24)): Decimal("5.0878")},
        stored={symbol: stored}, market_bar_factor={},
        sessions=[date(2026, 8, 12), date(2026, 8, 21), date(2026, 8, 24)])


class ManualReanchorCheckpointTests(unittest.TestCase):
    write_dates = [date(2026, 8, 21), date(2026, 8, 24)]

    def test_the_repair_keeps_an_operator_row_and_plans_no_replacement(self):
        plan = build_plan(_manual_inputs("manual_reanchor_rights_issue"), {},
                          write_dates=self.write_dates, rederive_derived=True)
        values = [row.adj_factor for day in self.write_dates for row in plan.rows[day]]
        self.assertEqual(values, [Decimal("5.0878")] * 2)
        for counts in plan_summary(plan)["dates"]:
            self.assertEqual((counts["replaces_other_value"], counts["fills_null"]), (0, 0), counts)

    def test_the_lanes_own_rows_are_still_recomputed_by_the_repair(self):
        plan = build_plan(_manual_inputs(lh.METHOD_VERSION), {},
                          write_dates=self.write_dates, rederive_derived=True)
        values = [row.adj_factor for day in self.write_dates for row in plan.rows[day]]
        self.assertNotIn(Decimal("5.0878"), values, "the lane re-derives its own rows unrounded")
        for value in values:
            self.assertAlmostEqual(float(value), 4.5917 * 5.23 / 4.72, places=4)


# --------------------------------------------------------------------------
# The real statements on real PostgreSQL, read-only, against VALUES fixtures
# --------------------------------------------------------------------------

_SUPERSEDED = ('{"superseded_at": "2026-09-19 15:16:41+08", "superseded_by": "longhu_qfq_derived", '
               '"superseded_reason": "misses the rights issue"}')
_LANE = f'{{"factor_semantics": "corporate_action_cumulative", "method": "{lh.METHOD_VERSION}"}}'
_MANUAL = '{"factor_semantics": "corporate_action_cumulative", "method": "manual_reanchor_rights_issue"}'

#: A superseded tushare row + the lane's derived row, bars not yet factored.
SUPERSEDED = "300999.SZ"
#: Control: the same tushare row WITHOUT the marker.
CONTROL = "300998.SZ"
#: The 300176 shape: superseded tushare + the operator's manual rows on the bars.
MANUAL = "300997.SZ"
#: A bar carrying a value only a superseded row supports.
ORPHAN = "300996.SZ"

_DAYS = ("2099-03-02", "2099-03-03", "2099-03-04")
_PRICES = {"2099-03-02": (5.23, 5.20), "2099-03-03": (4.80, 4.72), "2099-03-04": (4.85, 4.80)}


def _fixture_ctes() -> str:
    factors = []
    bars = []
    for symbol in (SUPERSEDED, CONTROL, MANUAL, ORPHAN):
        factors.append((symbol, _DAYS[0], "4.5917", "tushare_primary", "{}"))
        for day in _DAYS[1:]:
            factors.append((symbol, day, "4.5917", "tushare_primary",
                            "{}" if symbol == CONTROL else _SUPERSEDED))
            if symbol == SUPERSEDED:
                factors.append((symbol, day, "5.087837", lh.PROVIDER_KEY, _LANE))
            if symbol == MANUAL:
                factors.append((symbol, day, "5.0878", lh.PROVIDER_KEY, _MANUAL))
        for day in _DAYS:
            carried = ("4.5917" if day == _DAYS[0] or symbol == ORPHAN
                       else "5.0878" if symbol == MANUAL else "NULL")
            bars.append((symbol, day, *_PRICES[day], carried))
    factor_rows = ",\n".join(
        f"('{s}','{d}'::date,{v}::numeric,'{p}','2099-03-05 08:00+08'::timestamptz,'{r}'::jsonb)"
        for s, d, v, p, r in factors)
    bar_rows = ",\n".join(
        f"('{s}','{d}'::date,{c}::numeric,{pc}::numeric,{a}::numeric,'longhuvip_composite',"
        f"'fresh','2099-03-05 08:00+08'::timestamptz)" for s, d, c, pc, a in bars)
    calendar_rows = ",".join(f"('{d}'::date,true)" for d in _DAYS)
    return (
        f"fixture_factors(symbol,trading_date,adj_factor,provider,available_at,raw) AS (VALUES {factor_rows}),\n"
        f"fixture_bars(symbol,trading_date,close,pre_close,adj_factor,selected_provider,quality_status,"
        f"available_at) AS (VALUES {bar_rows}),\n"
        f"fixture_calendar(calendar_date,is_open) AS (VALUES {calendar_rows}),\n"
        f"fixture_market(symbol,trading_date,adj_factor) AS (VALUES ('{MANUAL}','2099-03-03'::date,5.0878::numeric))")


class _FixtureConnection:
    """Runs the module's own SQL with the quant tables shadowed by VALUES CTEs."""

    TABLES = {"quant.daily_adjustment_factors": "fixture_factors",
              "quant.canonical_bars_daily": "fixture_bars",
              "quant.market_trade_calendar": "fixture_calendar",
              "quant.market_bars_daily": "fixture_market"}

    def __init__(self, connection, symbols):
        self.connection = connection
        self.ctes = _fixture_ctes()
        self.symbols = symbols

    def execute(self, sql, params=None):
        for table, fixture in self.TABLES.items():
            sql = sql.replace(table, fixture)
        body = sql.lstrip()
        if body[:4].upper() == "WITH":
            sql = f"WITH {self.ctes},{body[4:]}"
        else:
            sql = f"WITH {self.ctes}\n{body}"
        return self.connection.execute(sql, params)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class SupersededEvidenceOnPostgresTests(unittest.TestCase):
    """Read-only by construction: the server refuses writes, and none are issued."""

    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.rows import dict_row

        from app.db_dsn import connection_params

        cls.connection = psycopg.connect(
            **connection_params(), row_factory=dict_row, connect_timeout=10,
            options="-c default_transaction_read_only=on")
        cls.fixture = _FixtureConnection(cls.connection, None)

    @classmethod
    def tearDownClass(cls):
        cls.connection.close()

    def _window(self, from_date):
        return lh.read_window(self.fixture, date.fromisoformat(from_date), date(2099, 3, 4),
                              symbols=[SUPERSEDED, CONTROL, MANUAL])

    def test_a_superseded_row_is_not_a_checkpoint_and_the_control_still_is(self):
        inputs = self._window("2099-03-03")
        superseded = inputs.stored.get(SUPERSEDED, {})
        self.assertEqual({(value.provider, value.exact) for value in superseded.values()},
                         {(lh.PROVIDER_KEY, Decimal("5.087837"))})
        control = inputs.stored[CONTROL]
        self.assertEqual({(value.provider, value.exact) for value in control.values()},
                         {("tushare_primary", Decimal("4.5917"))})
        manual = inputs.stored[MANUAL]
        self.assertTrue(all(value.manual and value.exact == Decimal("5.0878") for value in manual.values()))

    def test_a_superseded_row_is_not_an_anchor_and_the_control_still_is(self):
        inputs = self._window("2099-03-04")
        self.assertEqual((inputs.symbols[SUPERSEDED]["anchor_provider"],
                          inputs.symbols[SUPERSEDED]["anchor_factor"]),
                         (lh.PROVIDER_KEY, Decimal("5.087837")))
        self.assertEqual((inputs.symbols[CONTROL]["anchor_provider"],
                          inputs.symbols[CONTROL]["anchor_factor"]),
                         ("tushare_primary", Decimal("4.5917")))

    def test_the_repair_dry_run_plans_no_replacement_of_the_reanchored_bars(self):
        inputs = lh.read_window(self.fixture, date(2099, 3, 3), date(2099, 3, 4), symbols=[MANUAL])
        plan = build_plan(inputs, {}, write_dates=[date(2099, 3, 3), date(2099, 3, 4)],
                          rederive_derived=True)
        self.assertEqual([row.adj_factor for day in plan.write_dates for row in plan.rows[day]],
                         [Decimal("5.0878")] * 2)
        for counts in plan_summary(plan)["dates"]:
            self.assertEqual((counts["replaces_other_value"], counts["fills_null"]), (0, 0), counts)

    def test_a_bar_only_a_superseded_row_supports_is_a_leak_and_a_mismatch(self):
        mismatches = self.fixture.execute(
            factor_value_mismatch_sql("canonical_bars_daily"),
            (date(2099, 3, 2), date(2099, 3, 4))).fetchall()
        self.assertEqual(sorted((row["symbol"], str(row["trading_date"])) for row in mismatches),
                         [(ORPHAN, "2099-03-03"), (ORPHAN, "2099-03-04")])
        leaks = self.fixture.execute(identity_factor_leak_sql("canonical_bars_daily")).fetchone()
        self.assertEqual(int(leaks["identity_leaks"]), 2)


if __name__ == "__main__":
    unittest.main()
