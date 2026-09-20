from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from app.strategy_origin import select_primary_origin
from app.trade_thesis.scan_adapter import evaluate_source_run, intraday_evidence, market_evidence, scan_candidates, seed_thesis


CUTOFF = "2026-09-18T16:00:00+08:00"


def bar(day, amount, *, close=12.0, available="2026-09-18T15:10:00+08:00"):
    return {"trading_date": day, "close": close, "low": close - 0.2, "amount": amount,
            "available_at": available, "source_observation_ids": [f"bar-{day}"]}


def scan(lanes):
    return {"as_of_date": "2026-09-17", "lanes": lanes}


def lane(key, rank, origin, *, support=11.0, reference=12.5):
    return {"key": key, "total_matches": 94, "items": [{"symbol": "002185.SZ", "name": "华天科技",
            "rank": rank, "origin_id": origin, "support": support, "reference": reference,
            "reason": "原始结构观察", "confirmation": "原自由文本确认条件"}]}


def test_huatian_amount_ratios_keep_previous_and_mean5_baselines_separate():
    bars = [bar("2026-09-11", 31.0), bar("2026-09-14", 32.0), bar("2026-09-15", 33.0),
            bar("2026-09-16", 26.43), bar("2026-09-17", 43.22), bar("2026-09-18", 35.02)]
    days = [item["trading_date"] for item in bars]
    evidence = market_evidence("002185.SZ", bars, [], CUTOFF, "2026-09-18", days)
    values = {item["metric"]: item for item in evidence}
    assert round(values["amount_ratio_previous"]["value"], 2) == 0.81
    assert round(values["amount_ratio_mean5"]["value"], 2) == 1.06
    assert values["amount_ratio_previous"]["benchmark"] == "previous_session"
    assert values["amount_ratio_mean5"]["benchmark"] == "previous_5_sessions_mean"


def test_canonical_daily_amount_is_converted_from_thousand_cny_to_cny():
    # Real accepted canonical shape: 2,412,323.87 thousand CNY =
    # 2,412,323,870 CNY = 24.1232387亿元, not 0.024亿元.
    rows = [bar("2026-09-18", 2_412_323.87)]
    evidence = market_evidence("600988.SH", rows, [], CUTOFF, "2026-09-18", ["2026-09-18"])
    amount = next(item for item in evidence if item["metric"] == "amount")
    assert amount["unit"] == "CNY"
    assert amount["value"] == 2_412_323_870
    assert round(amount["value"] / 100_000_000, 4) == 24.1232


def test_late_current_bar_is_excluded_before_ratios_and_does_not_pollute_hash():
    bars = [bar("2026-09-17", 43.22, available="2026-09-17T15:10:00+08:00"),
            bar("2026-09-18", 35.02, available="2026-09-18T16:01:00+08:00")]
    assert market_evidence("002185.SZ", bars, [], CUTOFF, "2026-09-18") == []


def test_missing_session_never_bridges_amount_aggregates():
    # The adapter deliberately passes only the latest settled bar when its
    # exchange-calendar coverage check finds any missing session.
    incomplete = [bar("2026-09-11", 31.0), bar("2026-09-15", 33.0),
                  bar("2026-09-16", 26.43), bar("2026-09-17", 43.22), bar("2026-09-18", 35.02)]
    expected = ["2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    evidence = market_evidence("002185.SZ", incomplete, [], CUTOFF, "2026-09-18", expected)
    assert not {"amount_ratio_previous", "amount_ratio_mean5", "main_net5"} & {item["metric"] for item in evidence}


def test_scan_lane_reorder_keeps_primary_origin_and_seed_contract_stable():
    lanes = [lane("trend", 2, "trend-origin"), lane("expansion", 40, "expansion-origin")]
    first = scan_candidates(scan(lanes))["002185.SZ"]
    second = scan_candidates(scan(list(reversed(deepcopy(lanes)))))["002185.SZ"]
    assert select_primary_origin(first)["origin_id"] == "trend-origin"
    assert select_primary_origin(second)["origin_id"] == "trend-origin"
    deadline = "2026-09-21T15:00:00+08:00"
    a = seed_thesis(first, "run-1", "2026-09-17T16:00:00+08:00", CUTOFF, deadline)
    b = seed_thesis(second, "run-1", "2026-09-17T16:00:00+08:00", CUTOFF, deadline)
    assert a["primary_origin_id"] == b["primary_origin_id"] == "trend-origin"
    assert a["origin_primary_lane"] == b["origin_primary_lane"] == "trend"
    assert a["terminal_deadline"] == b["terminal_deadline"] == deadline


def test_seed_preserves_market_and_capture_times_as_distinct_scopes():
    row = scan_candidates(scan([lane("accumulation", 1, "origin-a")]))["002185.SZ"]
    thesis = seed_thesis(row, "run-historical", "2026-09-17T16:00:00+08:00", CUTOFF,
                         "2026-09-21T15:00:00+08:00")
    assert thesis["source_available_at"] == "2026-09-17T16:00:00+08:00"
    assert thesis["available_at"] == thesis["created_at"] == CUTOFF
    assert thesis["origin_mode"] == "reconstructed"
    assert thesis["terminal_deadline"] == "2026-09-21T15:00:00+08:00"
    assert thesis["entry_conditions"][0]["metric"] == "full_entry_scenario_confirmed"
    assert thesis["entry_conditions"][0]["basis"] == "scenario"


def test_explicit_empty_symbol_scope_never_expands_to_all_candidates():
    class Result:
        def __init__(self, rows): self.rows = rows
        def fetchone(self): return self.rows[0] if self.rows else None
        def fetchall(self): return self.rows

    class Connection:
        def execute(self, sql, _params=()):
            if "calendar_date>%s" in sql:
                return Result([{"calendar_date": date(2026, 9, 21)}])
            if "ORDER BY calendar_date DESC LIMIT 6" in sql:
                return Result([{"calendar_date": date(2026, 9, 18)}])
            return Result([])

    class Database:
        @contextmanager
        def transaction(self):
            yield Connection()

    source = {"kind": "post_close", "run_id": "run-1", "data_date": "2026-09-17",
              "available_at": "2026-09-17T16:00:00+08:00", "scan": scan([lane("trend", 1, "o1")])}
    with patch("app.trade_thesis.scan_adapter.load_source", return_value=source), \
         patch("app.trade_thesis.scan_adapter.list_latest", return_value=[]):
        try:
            evaluate_source_run(Database(), "run-1", cutoff_at=CUTOFF, symbols=[])
        except ValueError as error:
            assert str(error) == "no_matching_source_or_tracked_symbols"
        else:
            raise AssertionError("an explicit empty scope must not evaluate every candidate")


def test_intraday_observations_are_separate_from_settled_daily_conditions_and_ratios():
    item = {"source": "new_intraday", "cutoff_at": "2026-09-18T11:30:00+08:00",
            "price": 12.34, "amount": 18.5e8, "low": 11.92, "vwap": 12.18, "minute_end": "1130"}
    evidence = intraday_evidence("002185.SZ", item, CUTOFF, "2026-09-18T11:33:00+08:00")
    assert {row["metric"] for row in evidence} == {"price", "amount_so_far", "session_low_so_far", "vwap", "minute_end"}
    assert all(row["basis"] == "forming_intraday" for row in evidence)
    assert all(row["available_at"] == "2026-09-18T11:33:00+08:00" for row in evidence)
    assert all(row["effective_at"] == "2026-09-18T11:30:00+08:00" for row in evidence)
    assert all(row["minute_end"] == "1130" and row["source"] == "new_intraday" for row in evidence)
    assert not {"close", "amount", "amount_ratio_previous", "amount_ratio_mean5"} & {row["metric"] for row in evidence}
    assert next(row for row in evidence if row["metric"] == "amount_so_far")["value"] == 18.5e8


def test_intraday_observation_without_real_timestamp_or_after_cutoff_is_not_invented():
    item = {"price": 12.34, "amount": 18.5e8, "minute_end": "1130", "source": "new_intraday"}
    assert intraday_evidence("002185.SZ", item, CUTOFF, None) == []
    assert intraday_evidence("002185.SZ", item, CUTOFF, "2026-09-18T16:01:00+08:00") == []


def test_intraday_evidence_hash_changes_with_observation_time_and_source():
    item = {"price": 12.34, "amount": 18.5e8, "minute_end": "1130", "source": "new_intraday"}
    first = intraday_evidence("002185.SZ", item, CUTOFF, "2026-09-18T11:33:00+08:00")
    later = intraday_evidence("002185.SZ", item, CUTOFF, "2026-09-18T11:34:00+08:00")
    changed_source = intraday_evidence("002185.SZ", {**item, "source": "previous"}, CUTOFF,
                                       "2026-09-18T11:33:00+08:00")
    assert [row["evidence_id"] for row in first] != [row["evidence_id"] for row in later]
    assert [row["evidence_id"] for row in first] != [row["evidence_id"] for row in changed_source]
