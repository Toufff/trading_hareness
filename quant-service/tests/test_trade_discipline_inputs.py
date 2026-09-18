"""Evidence gathering for trade discipline: pure parts plus a fake connection.

No live database and no provider call.  The connection double answers by SQL
fragment the same way ``test_agent_paper`` does, so the real statements are the
ones exercised.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.trade_discipline.generator import CalendarInfo, GenerationInputs, generate
from app.trade_discipline.inputs import (
    account_equity,
    build_generation_inputs,
    canonical_json,
    closure_gaps,
    collect,
    forming_bar,
    gather_evidence,
    inputs_hash,
    lane_membership,
    live_forming_bar,
    merge_forming_bar,
    position_for,
    previous_active_plan,
    recommendation_note,
    sector_membership,
    settled_daily_bars,
    summarize_previous_plan,
    trading_calendar,
)
from app.trade_discipline.templates import closure_within

SH = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 18, 15, 30, tzinfo=SH)
SYMBOL = "600613.SH"
SNAPSHOT_ID = "11111111-1111-1111-1111-111111111111"


def sessions(count: int, end: date = date(2026, 9, 17)) -> list[date]:
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def settled_rows(count: int = 30) -> list[dict]:
    """A plain decaying series; the exact prices do not matter here, the plumbing does."""
    rows = []
    for index, day in enumerate(sessions(count)):
        close = 12.0 - index * 0.12
        rows.append({"symbol": SYMBOL, "trading_date": day, "open": close * 1.005, "high": close * 1.02,
                     "low": close * 0.98, "close": close, "pre_close": close * 1.01,
                     "volume": 3_000_000.0 + index * 1000, "amount": close * 3_000_000.0,
                     "is_suspended": False, "quality_status": "fresh"})
    return rows


class Result:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Answers by SQL fragment; anything unrecognised is an empty result, not a guess."""

    def __init__(self, **overrides):
        self.statements: list[tuple[str, tuple]] = []
        self.bars = overrides.get("bars", settled_rows())
        self.snapshot = overrides.get("snapshot", {
            "snapshot_id": SNAPSHOT_ID, "account_key": "citics-primary", "source": "broker_desktop",
            "source_snapshot_key": "bundle-sha", "observed_at": datetime(2026, 9, 18, 15, 5, tzinfo=SH),
            "verification": "verified_exact", "cash": Decimal("125.72"), "total_asset": Decimal("99632.00"),
            "total_market_value": Decimal("99506.28"), "content_hash": "a" * 64})
        self.positions = overrides.get("positions", [{
            "symbol": SYMBOL, "name": "神奇制药", "quantity": Decimal("5800"),
            "sellable_quantity": Decimal("5800"), "average_cost": Decimal("8.4907"),
            "market_price": Decimal("8.41"), "market_value": Decimal("48778.00"),
            "unrealized_pnl": Decimal("-457.06"), "position_weight_pct": Decimal("48.96")}])
        self.sector = overrides.get("sector", {"taxonomy_key": "longhu_ths_industry", "sector_key": "881140",
                                               "label": "化学制药", "effective_from": date(2026, 1, 2),
                                               "available_at": datetime(2026, 1, 2, tzinfo=SH)})
        self.sector_flow = overrides.get("sector_flow", {"change_pct": Decimal("-1.25"), "status": "ready"})
        self.scan = overrides.get("scan")
        self.candidate = overrides.get("candidate")
        self.pool = overrides.get("pool")
        self.calendar = overrides.get("calendar", [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
                                                   date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 28),
                                                   date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 9),
                                                   date(2026, 10, 12)])
        self.day_is_open = overrides.get("day_is_open", True)
        self.existing_tables = overrides.get("existing_tables", {"sector_flow_daily_features"})
        self.previous_plan_row = overrides.get("previous_plan_row")

    def execute(self, sql: str, params=()):  # noqa: C901 - a dispatch table, one branch per statement
        self.statements.append((" ".join(sql.split()), tuple(params)))
        if "information_schema.tables" in sql:
            return Result([{"present": 1}] if params[1] in self.existing_tables else [])
        if "quant.broker_portfolio_snapshots" in sql:
            return Result([self.snapshot] if self.snapshot else [])
        if "quant.broker_position_snapshots" in sql:
            return Result(list(self.positions))
        if "quant.canonical_bars_daily" in sql:
            return Result([row for row in self.bars if row["trading_date"] < params[1]][-params[2]:])
        if "quant.sector_membership_history" in sql:
            return Result([self.sector] if self.sector else [])
        if "quant.sector_flow_daily_features" in sql:
            return Result([self.sector_flow] if self.sector_flow else [])
        if "quant.intraday_strategy_scans" in sql:
            return Result([self.scan] if self.scan else [])
        if "quant.post_close_strategy_candidates" in sql:
            return Result([self.candidate] if self.candidate else [])
        if "quant.recommendation_pool_decisions" in sql:
            return Result([self.pool] if self.pool else [])
        if "quant.market_trade_calendar" in sql and "calendar_date>" in sql:
            return Result([{"calendar_date": day} for day in self.calendar][:params[2]])
        if "quant.market_trade_calendar" in sql:
            return Result([{"is_open": self.day_is_open}])
        if "quant.discipline_plans" in sql:
            return Result([self.previous_plan_row] if self.previous_plan_row else [])
        if "quant.instruments" in sql:
            return Result([{"name": "神奇制药"}])
        return Result([])

    def factory(self):
        connection = self

        class Scope:
            def __enter__(self):
                return connection

            def __exit__(self, *_args):
                return False
        return Scope()


class HashingTests(unittest.TestCase):
    def evidence(self) -> dict:
        connection = FakeConnection()
        return gather_evidence(connection, account_key="citics-primary", symbol=SYMBOL, as_of=AS_OF)

    def test_inputs_hash_is_deterministic_and_equals_the_generator_fingerprint(self):
        first = build_generation_inputs(run_id="run-a", account_key="citics-primary", symbol=SYMBOL,
                                        as_of=AS_OF, evidence=self.evidence())
        second = build_generation_inputs(run_id="run-a", account_key="citics-primary", symbol=SYMBOL,
                                         as_of=AS_OF, evidence=self.evidence())
        self.assertEqual(inputs_hash(first), inputs_hash(second))
        self.assertEqual(inputs_hash(first), first.fingerprint())
        self.assertRegex(inputs_hash(first), r"^[0-9a-f]{64}$")

    def test_any_changed_evidence_changes_the_hash(self):
        base = build_generation_inputs(run_id="run-a", account_key="citics-primary", symbol=SYMBOL,
                                       as_of=AS_OF, evidence=self.evidence())
        moved = self.evidence()
        moved["bars"][-1]["close"] += 0.01
        self.assertNotEqual(inputs_hash(base),
                            inputs_hash(build_generation_inputs(run_id="run-a", account_key="citics-primary",
                                                                symbol=SYMBOL, as_of=AS_OF, evidence=moved)))
        other_run = build_generation_inputs(run_id="run-b", account_key="citics-primary", symbol=SYMBOL,
                                            as_of=AS_OF, evidence=self.evidence())
        self.assertNotEqual(inputs_hash(base), inputs_hash(other_run))

    def test_canonical_json_is_key_ordered_and_keeps_chinese_readable(self):
        self.assertEqual(canonical_json({"b": 1, "a": "化学制药"}), '{"a": "化学制药", "b": 1}')


class CalendarGapTests(unittest.TestCase):
    def test_an_ordinary_weekend_is_two_closed_days_and_never_a_holiday_line(self):
        gaps = closure_gaps([date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)])
        self.assertEqual(gaps, [{"last_trading_date": "2026-09-18", "resume_date": "2026-09-21", "closed_days": 2}])

    def test_national_day_is_eight_closed_days(self):
        gaps = closure_gaps([date(2026, 9, 30), date(2026, 10, 9)])
        self.assertEqual(gaps[0]["closed_days"], 8)
        self.assertEqual(gaps[0]["resume_date"], "2026-10-09")

    def test_consecutive_sessions_produce_no_gap_and_order_is_repaired(self):
        self.assertEqual(closure_gaps([date(2026, 9, 22), date(2026, 9, 21)]), [])
        self.assertEqual(closure_gaps([]), [])

    def test_trading_calendar_anchors_the_first_gap_on_the_as_of_day(self):
        connection = FakeConnection()
        calendar, day_is_open = trading_calendar(connection, date(2026, 9, 18))
        self.assertTrue(day_is_open)
        self.assertEqual(calendar.upcoming_trading_dates[:5],
                         [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
                          date(2026, 9, 24), date(2026, 9, 25)])
        self.assertIn({"last_trading_date": "2026-09-18", "resume_date": "2026-09-21", "closed_days": 2},
                      calendar.closure_gaps)
        self.assertIn({"last_trading_date": "2026-09-30", "resume_date": "2026-10-09", "closed_days": 8},
                      calendar.closure_gaps)


    def test_a_closed_day_never_becomes_the_last_trading_date_of_a_holiday_gap(self):
        """Run on a Saturday inside the National Day break: the gap starts at a real session.

        Seeding the series with the closed day would name it as the holiday
        line's "last trading date", whose before-close deadline is already in
        the past, so the line would be born due.
        """
        connection = FakeConnection(day_is_open=False,
                                    calendar=[date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13)])
        calendar, day_is_open = trading_calendar(connection, date(2026, 10, 3))
        self.assertFalse(day_is_open)
        self.assertNotIn("2026-10-03", [gap["last_trading_date"] for gap in calendar.closure_gaps])
        for gap in calendar.closure_gaps:
            self.assertIn(date.fromisoformat(gap["last_trading_date"]), calendar.upcoming_trading_dates)

    def test_a_holiday_line_is_refused_when_its_last_session_is_not_an_open_one(self):
        stale = {"closure_gaps": [{"last_trading_date": "2026-10-03", "resume_date": "2026-10-09",
                                   "closed_days": 5}],
                 "sessions": ["2026-10-09", "2026-10-12"]}
        self.assertIsNone(closure_within(stale, "2026-10-12"))
        fresh = {"closure_gaps": [{"last_trading_date": "2026-09-30", "resume_date": "2026-10-09",
                                   "closed_days": 8}],
                 "sessions": ["2026-09-30", "2026-10-09"]}
        self.assertEqual(closure_within(fresh, "2026-10-09")["closed_days"], 8)


class FormingBarTests(unittest.TestCase):
    QUOTE = {"ts_code": SYMBOL, "name": "神奇制药", "price": 8.41, "pre_close": 8.30,
             "cumulative_volume_lot": 412_000, "cumulative_amount": 341_000_000.0}
    TAPE = {"open": 8.33, "high": 8.62, "low": 8.12, "last": 8.41, "vwap": 8.37}

    def test_forming_bar_widens_extremes_so_the_bar_survives_normalization(self):
        bar = forming_bar(date(2026, 9, 18), self.QUOTE, {**self.TAPE, "high": 8.20})
        self.assertEqual(bar["close"], 8.41)
        self.assertEqual(bar["high"], 8.41)      # last price above the minute high wins
        self.assertEqual(bar["low"], 8.12)
        self.assertTrue(bar["low"] <= bar["open"] <= bar["high"])
        self.assertEqual(bar["volume"], 41_200_000.0)
        self.assertEqual((bar["forming"], bar["synthetic"], bar["source"]), (True, False, "live_quote+minutes"))

    def test_quote_alone_and_tape_alone_both_work_and_nothing_fabricates_a_bar(self):
        self.assertEqual(forming_bar(date(2026, 9, 18), self.QUOTE, None)["source"], "live_quote")
        tape_only = forming_bar(date(2026, 9, 18), None, self.TAPE)
        self.assertEqual((tape_only["source"], tape_only["close"], tape_only["open"]), ("minutes", 8.41, 8.33))
        self.assertIsNone(forming_bar(date(2026, 9, 18), None, None))
        self.assertIsNone(forming_bar(date(2026, 9, 18), {"price": 0}, {"status": "fetch_failed"}))

    def test_merge_replaces_a_settled_row_for_the_same_date_and_keeps_order(self):
        settled = [{"trading_date": "2026-09-17", "close": 8.30},
                   {"trading_date": "2026-09-18", "close": 8.20}]
        merged, basis = merge_forming_bar(settled, forming_bar(date(2026, 9, 18), self.QUOTE, self.TAPE))
        self.assertEqual([row["trading_date"] for row in merged], ["2026-09-17", "2026-09-18"])
        self.assertEqual(merged[-1]["close"], 8.41)
        self.assertEqual(basis, {"bars_basis": "settled_plus_forming", "forming_source": "live_quote+minutes",
                                 "forming_date": "2026-09-18", "settled_bars": 2})

    def test_no_live_data_degrades_to_settled_only_instead_of_failing(self):
        settled = [{"trading_date": "2026-09-17", "close": 8.30}]
        merged, basis = merge_forming_bar(settled, None)
        self.assertEqual(merged, settled)
        self.assertEqual(basis["bars_basis"], "settled_only")
        self.assertIsNone(basis["forming_source"])

    def test_live_fetch_failures_are_absorbed(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("provider down")

        self.assertIsNone(asyncio.run(live_forming_bar(SYMBOL, date(2026, 9, 18),
                                                       fetch_quotes=boom, fetch_minute=boom)))

    def test_a_provider_status_payload_is_not_treated_as_a_tape(self):
        async def quotes(_symbols):
            return {SYMBOL: self.QUOTE}

        async def minutes(_symbols, _day):
            return {SYMBOL: {"status": "provider_session_2026-09-17"}}

        bar = asyncio.run(live_forming_bar(SYMBOL, date(2026, 9, 18), fetch_quotes=quotes, fetch_minute=minutes))
        self.assertEqual(bar["source"], "live_quote")


class DatabaseReadTests(unittest.TestCase):
    def test_settled_bars_stop_before_today_and_drop_suspended_sessions(self):
        connection = FakeConnection()
        connection.bars[-1]["is_suspended"] = True
        rows = settled_daily_bars(connection, SYMBOL, date(2026, 9, 18), count=10)
        self.assertTrue(all(row["trading_date"] < "2026-09-18" for row in rows))
        self.assertEqual(len(rows), 9)
        self.assertEqual(set(rows[0]) >= {"trading_date", "open", "high", "low", "close", "volume"}, True)
        self.assertEqual((rows[0]["synthetic"], rows[0]["forming"]), (False, False))
        self.assertEqual(connection.statements[-1][1], ([SYMBOL], date(2026, 9, 18), 10))

    def test_missing_sector_membership_returns_none_rather_than_a_guess(self):
        self.assertIsNone(sector_membership(FakeConnection(sector=None), SYMBOL, date(2026, 9, 18), AS_OF))
        found = sector_membership(FakeConnection(), SYMBOL, date(2026, 9, 18), AS_OF)
        self.assertEqual((found["code"], found["name"], found["taxonomy"]),
                         ("881140", "化学制药", "longhu_ths_industry"))

    def test_intraday_scan_wins_over_the_post_close_candidate(self):
        scan = {"run_id": "22222222-2222-2222-2222-222222222222", "cutoff": datetime(2026, 9, 18, 14, 55, tzinfo=SH),
                "result": {"lanes": [{"key": "reclaim", "items": [
                    {"symbol": SYMBOL, "lane": "reclaim", "state": "wait_confirmation",
                     "formal_state": "tracking", "reference": 8.54, "support": 8.04, "reason": "反包参考"}]}]}}
        candidate = {"run_id": "33333333-3333-3333-3333-333333333333", "candidate_type": "base_ready_30d",
                     "rank": 3, "score": Decimal("71"), "structure": {"metrics": {"resistance_price": 9.1,
                                                                                  "support_price": 7.9}},
                     "reason_codes": ["base"], "discovered_at": datetime(2026, 9, 17, 16, 0, tzinfo=SH)}
        lane = lane_membership(FakeConnection(scan=scan, candidate=candidate), SYMBOL, AS_OF)
        self.assertEqual((lane["source"], lane["lane"], lane["reference"], lane["support"], lane["formal_state"]),
                         ("intraday_strategy_scan", "reclaim", 8.54, 8.04, "tracking"))

    def test_post_close_candidate_is_the_fallback_and_absence_is_none(self):
        candidate = {"run_id": "33333333-3333-3333-3333-333333333333", "candidate_type": "base_ready_30d",
                     "rank": 3, "score": Decimal("71"),
                     "structure": {"metrics": {"resistance_price": 9.1, "support_price": 7.9}},
                     "reason_codes": ["base", "volume"], "discovered_at": datetime(2026, 9, 17, 16, 0, tzinfo=SH)}
        lane = lane_membership(FakeConnection(candidate=candidate), SYMBOL, AS_OF)
        self.assertEqual((lane["source"], lane["lane"], lane["reference"], lane["support"]),
                         ("post_close_strategy_candidates", "base_ready_30d", 9.1, 7.9))
        self.assertIsNone(lane_membership(FakeConnection(), SYMBOL, AS_OF))

    def test_recommendation_note_only_matches_this_symbol(self):
        pool = {"decision_id": "dec-1", "as_of_date": date(2026, 9, 17),
                "result": {"recommended": [{"symbol": "600664.SH", "priority": 1},
                                           {"symbol": SYMBOL, "priority": 2, "trigger": "站回8.70",
                                            "invalidation": "跌破8.04", "why_now": "急跌反抽", "stage": "反弹"}]}}
        note = recommendation_note(FakeConnection(pool=pool), SYMBOL, AS_OF)
        self.assertEqual((note["decision_id"], note["priority"], note["trigger"]), ("dec-1", 2, "站回8.70"))
        self.assertIsNone(recommendation_note(FakeConnection(pool=pool), "000001.SZ", AS_OF))

    def test_previous_plan_is_skipped_before_the_migration_is_applied(self):
        connection = FakeConnection(existing_tables=set())
        self.assertIsNone(previous_active_plan(connection, "citics-primary", SYMBOL))
        self.assertTrue(all("FROM quant.discipline_plans" not in sql for sql, _ in connection.statements))

    def test_previous_plan_reduces_to_the_two_numbers_the_ratchet_needs(self):
        row = {"plan_id": "44444444-4444-4444-4444-444444444444", "plan_key": "k", "as_of_at": AS_OF,
               "trading_date": date(2026, 9, 17), "stage": "crash_rebound", "status": "active",
               "sizing": {"hard_stop": "7.75"},
               "lines": [{"kind": "hard_stop", "action": {"type": "exit_all"}},
                         {"kind": "trail", "action": {"type": "move_stop_to", "value": "8.04"}}]}
        self.assertEqual(summarize_previous_plan(row)["hard_stop"], 7.75)
        self.assertEqual(summarize_previous_plan(row)["trail"], 8.04)
        connection = FakeConnection(existing_tables={"discipline_plans"}, previous_plan_row=row)
        self.assertEqual(previous_active_plan(connection, "citics-primary", SYMBOL)["plan_id"], row["plan_id"])

    def test_equity_prefers_total_asset_and_falls_back_to_cash_plus_market_value(self):
        self.assertEqual(account_equity({"cash": Decimal("1"), "total_asset": Decimal("10"),
                                         "total_market_value": Decimal("8")}), (Decimal("10"), Decimal("1")))
        self.assertEqual(account_equity({"cash": Decimal("1"), "total_asset": None,
                                         "total_market_value": Decimal("8")}), (Decimal("9"), Decimal("1")))
        self.assertEqual(account_equity(None), (None, None))

    def test_position_lookup_ignores_a_zeroed_holding(self):
        rows = [{"symbol": SYMBOL, "quantity": 0}, {"symbol": "600664.SH", "quantity": 100}]
        self.assertIsNone(position_for(rows, SYMBOL))
        self.assertEqual(position_for(rows, "600664.SH")["quantity"], 100)


class EndToEndAssemblyTests(unittest.TestCase):
    def test_gathered_evidence_generates_a_plan_whose_hash_matches_its_inputs(self):
        connection = FakeConnection()
        inputs = asyncio.run(collect(connection.factory, run_id="55555555-5555-5555-5555-555555555555",
                                     account_key="citics-primary", symbol=SYMBOL, as_of=AS_OF, allow_live=False))
        self.assertIsInstance(inputs, GenerationInputs)
        self.assertEqual(inputs.name, "神奇制药")
        self.assertEqual(inputs.equity, Decimal("99632.00"))
        self.assertEqual(inputs.position["quantity"], 5800)
        self.assertEqual(inputs.sector["code"], "881140")
        self.assertEqual(inputs.sector["change_pct"], -1.25)
        self.assertIn("bars_basis:settled_only", inputs.evidence_refs)
        self.assertIsInstance(inputs.calendar, CalendarInfo)

        plan = generate(inputs)
        self.assertEqual(plan.inputs_hash, inputs_hash(inputs))
        self.assertEqual(plan.plan_kind, "holding")
        self.assertEqual(plan.valid_until, datetime(2026, 9, 25, 15, 0, tzinfo=SH))
        self.assertIn(plan.status, {"active", "rejected_by_quality"})
        self.assertTrue(plan.lines_of("hard_stop"))

    def test_an_open_session_merges_the_forming_bar_into_the_frozen_inputs(self):
        connection = FakeConnection()

        async def quotes(_symbols):
            return {SYMBOL: {"price": 8.41, "cumulative_volume_lot": 412_000, "cumulative_amount": 3.41e8}}

        async def minutes(_symbols, _day):
            return {SYMBOL: {"open": 8.33, "high": 8.62, "low": 8.12, "last": 8.41, "vwap": 8.37}}

        inputs = asyncio.run(collect(connection.factory, run_id="66666666-6666-6666-6666-666666666666",
                                     account_key="citics-primary", symbol=SYMBOL, as_of=AS_OF,
                                     fetch_quotes=quotes, fetch_minute=minutes))
        self.assertEqual(inputs.bars[-1]["trading_date"], "2026-09-18")
        self.assertTrue(inputs.bars[-1]["forming"])
        self.assertIn("forming_bar:live_quote+minutes:2026-09-18", inputs.evidence_refs)

    def test_a_closed_session_never_asks_the_provider(self):
        connection = FakeConnection(day_is_open=False)

        async def boom(*_args, **_kwargs):
            raise AssertionError("must not fetch on a closed session")

        inputs = asyncio.run(collect(connection.factory, run_id="77777777-7777-7777-7777-777777777777",
                                     account_key="citics-primary", symbol=SYMBOL, as_of=AS_OF,
                                     fetch_quotes=boom, fetch_minute=boom))
        self.assertIn("bars_basis:settled_only", inputs.evidence_refs)


if __name__ == "__main__":
    unittest.main()
