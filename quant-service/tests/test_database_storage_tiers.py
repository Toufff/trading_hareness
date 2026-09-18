"""Pure-function and SQL-shape coverage for scripts/database-storage-tiers.py.

The tiering job deletes rows from the hot tables, so the decisions it makes --
where the cutoff falls, which tables a space shortage takes history from, how
far the hot window may ever shrink, when it stops -- are the part that must be
provable without a cluster.  The script is imported by path (the pattern
``test_migration_contracts.py`` uses for the migration modules); every psycopg
import inside it is lazy, so importing it here needs no database driver.

Three groups of tests do need more than pure functions and get it without a
database:

* ``DirectorySizeTest`` builds a real directory tree under a temp root and, on
  Windows, a real junction with ``mklink /J``.  That junction is the defect the
  review found: ``PGDATA\\pg_tblspc\\<oid>`` points at the cold tablespace on
  G:, and a walk that follows it measures the cold tier as hot usage.
* ``MoveStatementShapeTest`` renders the move statements with psycopg's SQL
  composer against a fake column list and asserts the shape the safety argument
  depends on: explicit column lists, insert before delete, delete by primary
  key, no ``SELECT *`` and no bare ``ON CONFLICT DO NOTHING`` swallowing a
  differing row.
* ``CutoffIndexMigrationTest`` pins the new Alembic migration against the
  script's policy so ``install`` and a rebuilt database create the same five
  index names.

``ScratchDatabaseMoveTest`` does need a cluster and says so: it is skipped
unless ``STORAGE_TIERS_SCRATCH_TEST=1`` and the usual ``PG*`` variables are in
the environment, and it then creates its own database, runs ``_move_table``
against it (conflict, clamp, ``FOR UPDATE``) and drops it again.  Everything the
default run asserts about the move is a *statement shape*; the behaviour is
executed only by that test and by the branch's end-to-end script.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import alembic.op

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "database-storage-tiers.py"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

GB = 1024**3


def load_module():
    spec = importlib.util.spec_from_file_location("database_storage_tiers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its annotations through sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tiers = load_module()


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TierPolicyTest(unittest.TestCase):
    """The policy itself is a contract: the docs and the guard test read it."""

    def test_five_tiered_tables_with_a_one_year_hot_window(self):
        self.assertEqual(
            [(policy.qualified, policy.column, policy.hot_days) for policy in tiers.TIER_POLICY],
            [
                ("quant.raw_market_observations", "available_at", 365),
                ("quant.tushare_raw_records", "available_at", 365),
                ("quant.intraday_quote_observations", "observed_at", 365),
                ("quant.intraday_rule_input_snapshots", "observed_at", 365),
                ("quant.edge_evidence_changes", "changed_at", 365),
            ],
        )

    def test_twin_view_and_cutoff_index_names_are_derived_not_configured(self):
        policy = tiers.TIER_POLICY[0]
        self.assertEqual(policy.cold_table, "quant.raw_market_observations_cold")
        self.assertEqual(policy.all_view, "quant.raw_market_observations_all")
        self.assertEqual(policy.cutoff_index, "raw_market_observations_tier_cutoff_idx")

    def test_whole_table_cold_placement_covers_the_legacy_archive(self):
        self.assertEqual(tiers.WHOLE_TABLE_COLD, ("quant.legacy_source_records",))

    def test_only_the_peer_role_gets_bounded_timeouts(self):
        self.assertEqual({role for role, _, _ in tiers.ROLE_SETTINGS}, {"stock_peer"})
        self.assertEqual(
            dict((setting, value) for _, setting, value in tiers.ROLE_SETTINGS),
            {"statement_timeout": "15min", "idle_in_transaction_session_timeout": "5min"},
        )

    def test_with_hot_days_returns_a_new_policy(self):
        policy = tiers.TIER_POLICY[0]
        shortened = policy.with_hot_days(30)
        self.assertEqual(shortened.hot_days, 30)
        self.assertEqual(policy.hot_days, 365, "the frozen policy must not be mutated")
        self.assertEqual(shortened.column, policy.column)

    def test_the_quarantine_table_is_not_a_cold_twin(self):
        # It holds the only copy of a hot row that could not be moved, so it
        # must stay in the default tablespace and out of the dump exclusions.
        self.assertEqual(tiers.QUARANTINE_TABLE, "quant.storage_tier_conflicts")
        self.assertFalse(tiers.QUARANTINE_TABLE.endswith("_cold"))


class HotCutoffTest(unittest.TestCase):
    def test_cutoff_is_exactly_hot_days_before_now(self):
        now = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(tiers.hot_cutoff(now, 365), now - timedelta(days=365))
        self.assertEqual(tiers.hot_cutoff(now, 30), datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc))

    def test_a_naive_now_is_refused(self):
        # A naive cutoff would mean "local time" against a timestamptz column and
        # would move up to eight hours of rows that are still inside the window.
        with self.assertRaises(ValueError):
            tiers.hot_cutoff(datetime(2026, 9, 19, 6, 0), 365)

    def test_a_non_positive_hot_window_is_refused(self):
        now = datetime.now(timezone.utc)
        for days in (0, -1):
            with self.assertRaises(ValueError):
                tiers.hot_cutoff(now, days)

    def test_a_non_datetime_is_refused(self):
        with self.assertRaises(TypeError):
            tiers.hot_cutoff("2026-09-19", 365)


class DeadlineTest(unittest.TestCase):
    """The 06:00 job must stop before the pre-open jobs want the disk."""

    TZ = timezone(timedelta(hours=8))  # Asia/Shanghai, the task host's clock

    def test_no_deadline_when_neither_option_is_given(self):
        now = datetime(2026, 9, 19, 6, 0, tzinfo=self.TZ)
        self.assertIsNone(tiers.resolve_deadline(now))

    def test_a_clock_time_later_today_is_today(self):
        now = datetime(2026, 9, 19, 6, 0, tzinfo=self.TZ)
        self.assertEqual(
            tiers.resolve_deadline(now, "08:00"), datetime(2026, 9, 19, 8, 0, tzinfo=self.TZ)
        )

    def test_a_clock_time_just_behind_us_is_already_reached(self):
        # A replayed 06:00 run that Windows starts at 09:00 must write its
        # receipt and stop, not wait 23 hours for tomorrow's 08:00.
        now = datetime(2026, 9, 19, 9, 0, tzinfo=self.TZ)
        self.assertEqual(
            tiers.resolve_deadline(now, "08:00"), datetime(2026, 9, 19, 8, 0, tzinfo=self.TZ)
        )

    def test_a_clock_time_more_than_twelve_hours_behind_is_tomorrow(self):
        now = datetime(2026, 9, 19, 23, 0, tzinfo=self.TZ)
        self.assertEqual(
            tiers.resolve_deadline(now, "08:00"), datetime(2026, 9, 20, 8, 0, tzinfo=self.TZ)
        )

    def test_max_seconds_alone_and_the_earlier_of_the_two_wins(self):
        now = datetime(2026, 9, 19, 6, 0, tzinfo=self.TZ)
        self.assertEqual(tiers.resolve_deadline(now, None, 600), now + timedelta(seconds=600))
        self.assertEqual(tiers.resolve_deadline(now, "08:00", 600), now + timedelta(seconds=600))
        self.assertEqual(
            tiers.resolve_deadline(now, "08:00", 36_000), datetime(2026, 9, 19, 8, 0, tzinfo=self.TZ)
        )

    def test_a_naive_now_or_a_broken_clock_is_refused(self):
        with self.assertRaises(ValueError):
            tiers.resolve_deadline(datetime(2026, 9, 19, 6, 0), "08:00")
        for value in ("8", "25:00", "08:60", "eight", ""):
            with self.assertRaises(ValueError):
                tiers.parse_clock(value)
        with self.assertRaises(ValueError):
            tiers.resolve_deadline(datetime(2026, 9, 19, 6, 0, tzinfo=self.TZ), None, -1)

    def test_deadline_reached_exits_zero_so_the_next_night_resumes(self):
        # The move is idempotent and resumable; a deadline is a normal ending.
        self.assertEqual(tiers.exit_code_for("deadline_reached"), 0)
        self.assertEqual(tiers.exit_code_for("ok"), 0)


class ApplyStatusTest(unittest.TestCase):
    """One run, several things wrong: the receipt must name the worst of them."""

    @staticmethod
    def _record(tables=(), errors=(), space="ok", quarantined=0, deadline=False, missed=False):
        return {
            "tables": [{"status": status} for status in tables],
            "errors": list(errors),
            "space_policy": {"status": space},
            "quarantined_rows": quarantined,
            "deadline_reached": deadline,
            "deadline_missed": missed,
        }

    def test_a_clean_run_is_ok(self):
        self.assertEqual(tiers._apply_status(self._record(tables=["ok", "ok"])), "ok")

    def test_a_deadline_is_a_normal_ending(self):
        self.assertEqual(tiers._apply_status(self._record(tables=["ok"], deadline=True)), "deadline_reached")

    def test_a_quarantined_row_outranks_a_deadline(self):
        record = self._record(tables=["conflicts"], quarantined=3, deadline=True)
        self.assertEqual(tiers._apply_status(record), "conflicts")

    def test_a_per_table_failure_outranks_a_conflict(self):
        record = self._record(tables=["failed"], errors=[{"table": "x"}], quarantined=1)
        self.assertEqual(tiers._apply_status(record), "partial")

    def test_schema_drift_outranks_a_plain_failure(self):
        # Every other per-table failure is retried harmlessly tomorrow; a twin
        # whose columns moved needs a human before the next run.
        record = self._record(tables=["schema_drift", "failed"], errors=[{"table": "x"}])
        self.assertEqual(tiers._apply_status(record), "schema_drift")

    def test_an_unenforced_budget_outranks_everything(self):
        for space in sorted(tiers.DEGRADED_SPACE_STATUSES):
            record = self._record(tables=["schema_drift"], errors=[{"t": 1}], space=space, quarantined=2)
            self.assertEqual(tiers._apply_status(record), "degraded", space)

    def test_a_working_space_status_is_not_degraded(self):
        for space in ("ok", "reduce", "capped"):
            self.assertEqual(tiers._apply_status(self._record(tables=["ok"], space=space)), "ok", space)

    def test_a_deadline_already_past_at_the_start_is_not_a_normal_ending(self):
        # The machine was off at 06:00 and Windows starts the task on a Saturday
        # at 10:00: the deadline chain resolves 08:00 today, the loop breaks on
        # the first table and the old code exited 0 with 'deadline_reached',
        # indistinguishable in the log from a run that worked until 08:00.
        record = self._record(tables=[], deadline=True, missed=True)
        self.assertEqual(tiers._apply_status(record), "deadline_missed")
        self.assertEqual(tiers.exit_code_for("deadline_missed"), 1)

    def test_an_uninstalled_tier_is_never_a_green_night(self):
        # This is today's production state: no twins, no quarantine table. The
        # job moves nothing at all and the scheduled task used to show success.
        for status in sorted(tiers.NOT_INSTALLED_TABLE_STATUSES):
            self.assertEqual(tiers._apply_status(self._record(tables=[status, "ok"])), "partial", status)
            self.assertEqual(tiers.exit_code_for("partial"), 1)

    def test_a_broken_backup_chain_or_an_unsupported_index_needs_a_human(self):
        for status in sorted(tiers.BLOCKING_TABLE_STATUSES):
            self.assertEqual(tiers._apply_status(self._record(tables=[status])), "partial", status)

    def test_chain_behind_is_a_working_state_not_a_failure(self):
        # The clamp did exactly its job; the run is still 'ok' and exits 0. It
        # alerts (see the alert ladder) so nobody has to read the receipt.
        self.assertEqual(tiers._apply_status(self._record(tables=["chain_behind", "ok"])), "ok")
        self.assertNotIn("chain_behind", tiers.BLOCKING_TABLE_STATUSES)

    def test_the_two_skipped_statuses_are_exactly_the_uninstalled_ones(self):
        self.assertEqual(
            tiers.NOT_INSTALLED_TABLE_STATUSES,
            frozenset({"skipped_missing_table", "skipped_missing_quarantine"}),
        )


class ExitCodeTest(unittest.TestCase):
    def test_an_unenforced_budget_is_a_failure_not_a_green_night(self):
        for status in ("degraded", "failed"):
            self.assertEqual(tiers.exit_code_for(status), 2, status)
        for status in ("partial", "conflicts", "schema_drift"):
            self.assertEqual(tiers.exit_code_for(status), 1, status)
        self.assertEqual(tiers.exit_code_for("something-new"), 1, "an unknown status is never a success")

    def test_the_degraded_space_statuses_are_the_ones_that_mean_no_guard(self):
        self.assertEqual(tiers.DEGRADED_SPACE_STATUSES, frozenset({"unknown", "exhausted", "needs_repack"}))


class SelectTablesTest(unittest.TestCase):
    def test_no_selection_means_every_tiered_table(self):
        self.assertEqual(tiers.select_tables(tiers.TIER_POLICY), tiers.TIER_POLICY)
        self.assertEqual(tiers.select_tables(tiers.TIER_POLICY, []), tiers.TIER_POLICY)

    def test_bare_and_qualified_names_both_resolve(self):
        chosen = tiers.select_tables(tiers.TIER_POLICY, ["edge_evidence_changes", "quant.tushare_raw_records"])
        self.assertEqual([p.qualified for p in chosen], ["quant.edge_evidence_changes", "quant.tushare_raw_records"])

    def test_names_are_deduplicated_and_whitespace_tolerant(self):
        chosen = tiers.select_tables(
            tiers.TIER_POLICY, [" quant.edge_evidence_changes ", "EDGE_EVIDENCE_CHANGES"]
        )
        self.assertEqual([p.qualified for p in chosen], ["quant.edge_evidence_changes"])

    def test_an_unknown_table_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(ValueError) as caught:
            tiers.select_tables(tiers.TIER_POLICY, ["quant.canonical_bars_daily"])
        self.assertIn("quant.raw_market_observations", str(caught.exception))


class ParseBytesTest(unittest.TestCase):
    def test_units_and_bare_counts(self):
        self.assertEqual(tiers.parse_bytes("500GB"), 500 * GB)
        self.assertEqual(tiers.parse_bytes("500 gb"), 500 * GB)
        self.assertEqual(tiers.parse_bytes("2TB"), 2 * 1024**4)
        self.assertEqual(tiers.parse_bytes("1024"), 1024)
        self.assertEqual(tiers.parse_bytes("1024B"), 1024)
        self.assertEqual(tiers.parse_bytes(4096), 4096)

    def test_missing_value_falls_back_to_the_default(self):
        self.assertEqual(tiers.parse_bytes(None, 7), 7)
        self.assertEqual(tiers.parse_bytes("  ", 7), 7)
        self.assertIsNone(tiers.parse_bytes(None))

    def test_garbage_and_non_positive_sizes_are_refused(self):
        for value in ("abc", "GB", "-5GB", "0", "0GB"):
            with self.assertRaises(ValueError):
                tiers.parse_bytes(value)

    def test_the_documented_default_budget_is_500gb(self):
        self.assertEqual(tiers.DEFAULT_BUDGET_BYTES, 500 * GB)


class EffectiveBudgetTest(unittest.TestCase):
    """PGDATA_BUDGET_BYTES is an assumption; the volume is the fact."""

    def test_a_volume_that_can_hold_the_budget_leaves_it_alone(self):
        budget = tiers.effective_budget(500 * GB, 100 * GB, 900 * GB)
        self.assertEqual(budget["effective_bytes"], 500 * GB)
        self.assertTrue(budget["fits_volume"])
        self.assertEqual(budget["capacity_bytes"], 1000 * GB)

    def test_a_volume_too_small_for_the_budget_lowers_it_and_says_so(self):
        # 480 GB drive, 500 GB budget: without this the job reports "within the
        # high-water mark" right up to the moment PostgreSQL PANICs.
        budget = tiers.effective_budget(500 * GB, 80 * GB, 400 * GB)
        self.assertEqual(budget["effective_bytes"], 480 * GB)
        self.assertFalse(budget["fits_volume"])

    def test_an_exactly_fitting_volume_fits(self):
        self.assertTrue(tiers.effective_budget(500 * GB, 100 * GB, 400 * GB)["fits_volume"])

    def test_an_unknown_volume_keeps_the_configured_budget_and_says_nothing(self):
        budget = tiers.effective_budget(500 * GB, None, None)
        self.assertEqual(budget["effective_bytes"], 500 * GB)
        self.assertIsNone(budget["fits_volume"])

    def test_a_non_positive_configured_budget_is_refused(self):
        with self.assertRaises(ValueError):
            tiers.effective_budget(0, 1, 1)


class PlanSpaceMovesTest(unittest.TestCase):
    SIZES = {"quant.a": 40 * GB, "quant.b": 10 * GB}
    DAYS = {"quant.a": 400, "quant.b": 400}

    def test_usage_below_the_high_water_mark_plans_nothing(self):
        verdict = tiers.plan_space_moves(80 * GB, 100 * GB, self.SIZES, self.DAYS)
        self.assertEqual(verdict["status"], "ok")
        self.assertEqual(verdict["steps"], [])
        self.assertEqual(verdict["table_hot_days"], {})
        self.assertFalse(verdict["alert"])
        self.assertEqual(verdict["estimated_usage_bytes_after"], 80 * GB)

    def test_exactly_at_the_high_water_mark_is_still_ok(self):
        self.assertEqual(tiers.plan_space_moves(85 * GB, 100 * GB, self.SIZES, self.DAYS)["status"], "ok")

    def test_one_run_never_takes_more_than_the_day_cap_from_a_table(self):
        # The defect: an estimate-driven loop went from 365 hot days to 30 in a
        # single run.  Seven days a table a run, then come back tomorrow.
        verdict = tiers.plan_space_moves(90 * GB, 100 * GB, self.SIZES, self.DAYS)
        self.assertEqual(verdict["status"], "capped")
        self.assertEqual(verdict["max_days_per_table"], 7)
        self.assertEqual(verdict["table_hot_days"], {"quant.a": 393, "quant.b": 393})
        self.assertEqual(len(verdict["steps"]), 14)
        self.assertIn("per-run maximum", verdict["reason"])

    def test_the_default_day_cap_is_seven(self):
        self.assertEqual(tiers.DEFAULT_MAX_SPACE_DAYS, 7)

    def test_without_the_cap_the_plan_walks_to_the_target(self):
        verdict = tiers.plan_space_moves(90 * GB, 100 * GB, self.SIZES, self.DAYS, max_days_per_table=1000)
        self.assertEqual(verdict["status"], "reduce")
        self.assertFalse(verdict["alert"])
        # 40 GB over a 400 day span is 0.1 GB a day; 15 GB back under the 75 %
        # target is 150 days, and only the larger table is touched.
        self.assertEqual(verdict["table_hot_days"], {"quant.a": 250})
        self.assertEqual(len(verdict["steps"]), 150)
        self.assertEqual({step["table"] for step in verdict["steps"]}, {"quant.a"})
        self.assertLessEqual(verdict["estimated_usage_bytes_after"], 75 * GB)
        self.assertEqual(verdict["steps"][-1]["hot_days"], 250)

    def test_a_small_shortfall_is_met_inside_the_cap_and_reports_reduce(self):
        # 4400 GB over 400 days is 11 GB a day, and 86 % -> 75 % is 11 GB.
        verdict = tiers.plan_space_moves(86 * GB, 100 * GB, {"quant.a": 4400 * GB}, {"quant.a": 400})
        self.assertEqual(verdict["status"], "reduce")
        self.assertEqual(len(verdict["steps"]), 1)
        self.assertEqual(verdict["table_hot_days"], {"quant.a": 399})

    def test_the_hot_window_never_drops_below_thirty_days_and_alerts_instead(self):
        verdict = tiers.plan_space_moves(99 * GB, 100 * GB, {"quant.a": 10 * GB}, {"quant.a": 31})
        self.assertEqual(verdict["status"], "exhausted")
        self.assertTrue(verdict["alert"])
        self.assertEqual(verdict["table_hot_days"], {"quant.a": 30})
        self.assertIn("30", verdict["reason"])

    def test_a_table_already_at_the_floor_is_never_selected(self):
        verdict = tiers.plan_space_moves(99 * GB, 100 * GB, {"quant.a": 10 * GB}, {"quant.a": 30})
        self.assertEqual(verdict["status"], "exhausted")
        self.assertEqual(verdict["steps"], [])
        self.assertTrue(verdict["alert"])

    def test_past_the_alert_ratio_the_run_is_flagged_even_when_it_can_recover(self):
        verdict = tiers.plan_space_moves(96 * GB, 100 * GB, self.SIZES, self.DAYS, max_days_per_table=1000)
        self.assertEqual(verdict["status"], "reduce")
        self.assertTrue(verdict["alert"])

    def test_tables_without_a_known_oldest_row_are_not_candidates(self):
        verdict = tiers.plan_space_moves(90 * GB, 100 * GB, {"quant.a": 40 * GB}, {"quant.a": None})
        self.assertEqual(verdict["status"], "exhausted")
        self.assertEqual(verdict["steps"], [])

    def test_empty_tables_are_not_candidates(self):
        verdict = tiers.plan_space_moves(90 * GB, 100 * GB, {"quant.a": 0}, {"quant.a": 400})
        self.assertEqual(verdict["status"], "exhausted")
        self.assertEqual(verdict["steps"], [])

    def test_the_plan_is_deterministic_and_does_not_mutate_its_inputs(self):
        sizes = dict(self.SIZES)
        days = dict(self.DAYS)
        first = tiers.plan_space_moves(90 * GB, 100 * GB, sizes, days)
        second = tiers.plan_space_moves(90 * GB, 100 * GB, sizes, days)
        self.assertEqual(first, second)
        self.assertEqual(sizes, self.SIZES)
        self.assertEqual(days, self.DAYS)

    def test_equal_sized_tables_break_the_tie_by_name(self):
        verdict = tiers.plan_space_moves(
            90 * GB, 100 * GB, {"quant.b": 40 * GB, "quant.a": 40 * GB}, {"quant.a": 400, "quant.b": 400}
        )
        self.assertEqual(verdict["steps"][0]["table"], "quant.b", "the later name wins a size tie")

    def test_a_non_positive_budget_negative_usage_or_zero_cap_is_refused(self):
        with self.assertRaises(ValueError):
            tiers.plan_space_moves(1, 0, {}, {})
        with self.assertRaises(ValueError):
            tiers.plan_space_moves(-1, 100 * GB, {}, {})
        with self.assertRaises(ValueError):
            tiers.plan_space_moves(90 * GB, 100 * GB, self.SIZES, self.DAYS, max_days_per_table=0)

    def test_thresholds_match_the_documented_policy(self):
        self.assertEqual(
            (tiers.HIGH_WATER_RATIO, tiers.TARGET_RATIO, tiers.ALERT_RATIO, tiers.MIN_HOT_DAYS),
            (0.85, 0.75, 0.95, 30),
        )
        self.assertEqual(tiers.MIN_USAGE_DROP_RATIO, 0.01)


class SchemaDriftTest(unittest.TestCase):
    """The twin is filled by name; a column list that moved must stop the move."""

    HOT = [("id", "uuid"), ("observed_at", "timestamp with time zone"), ("payload", "jsonb")]

    def test_identical_column_lists_are_not_drift(self):
        drift = tiers.schema_drift(self.HOT, list(self.HOT))
        self.assertFalse(drift["blocking"])
        self.assertFalse(drift["repairable"])

    def test_column_order_alone_is_not_drift(self):
        drift = tiers.schema_drift(self.HOT, list(reversed(self.HOT)))
        self.assertFalse(drift["blocking"])

    def test_a_new_hot_column_is_repairable_drift(self):
        drift = tiers.schema_drift(self.HOT + [("source", "text")], self.HOT)
        self.assertTrue(drift["blocking"])
        self.assertTrue(drift["repairable"])
        self.assertEqual(drift["missing_in_cold"], ["source"])

    def test_a_column_only_the_twin_has_is_not_repairable(self):
        drift = tiers.schema_drift(self.HOT, self.HOT + [("legacy", "text")])
        self.assertTrue(drift["blocking"])
        self.assertFalse(drift["repairable"])
        self.assertEqual(drift["extra_in_cold"], ["legacy"])

    def test_a_type_that_moved_underneath_is_not_repairable(self):
        cold = [("id", "uuid"), ("observed_at", "timestamp without time zone"), ("payload", "jsonb")]
        drift = tiers.schema_drift(self.HOT, cold)
        self.assertTrue(drift["blocking"])
        self.assertFalse(drift["repairable"])
        self.assertEqual(drift["type_mismatch"][0]["column"], "observed_at")


class IncrementalSpecTest(unittest.TestCase):
    """Same grammar and same default as Get-StockIncrementalTableSpecs (psm1)."""

    def test_the_documented_default_covers_raw_market_observations(self):
        self.assertEqual(
            tiers.parse_incremental_specs(None),
            {"quant.raw_market_observations": ("created_at", "updated_at")},
        )
        self.assertEqual(tiers.parse_incremental_specs(""), tiers.parse_incremental_specs(None))
        self.assertEqual(
            tiers.DEFAULT_INCREMENTAL_TABLES, "quant.raw_market_observations:created_at:updated_at"
        )

    def test_none_disables_the_chain_and_therefore_the_clamp(self):
        for value in ("none", "NONE", " None "):
            self.assertEqual(tiers.parse_incremental_specs(value), {}, value)

    def test_several_tables_with_and_without_an_update_column(self):
        self.assertEqual(
            tiers.parse_incremental_specs("quant.a:created_at;quant.b:ts:touched_at"),
            {"quant.a": ("created_at", None), "quant.b": ("ts", "touched_at")},
        )

    def test_a_malformed_or_duplicated_spec_is_refused_not_guessed(self):
        # Guessing would turn the clamp off for the one table it protects.
        for value in ("raw_market_observations:created_at", "quant.a", "quant.a:", "quant.a:b:c:d",
                      "quant.a:x;quant.a:y"):
            with self.assertRaises(ValueError, msg=value):
                tiers.parse_incremental_specs(value)


class ChainWatermarkTest(unittest.TestCase):
    """The dump excludes a twin's data; the chain's watermark is why that is safe."""

    def test_the_psm1_state_shape_parses_to_an_aware_utc_instant(self):
        watermark = tiers.parse_chain_watermark(
            {"table": "quant.raw_market_observations", "watermark": "2026-09-18T12:00:03.098279Z",
             "updated_at": "2026-09-18T20:30:42.0022860+08:00"}
        )
        self.assertEqual(watermark, datetime(2026, 9, 18, 12, 0, 3, 98279, tzinfo=timezone.utc))

    def test_an_offset_watermark_is_normalised_to_utc(self):
        self.assertEqual(
            tiers.parse_chain_watermark({"watermark": "2026-09-18T20:00:00+08:00"}),
            datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc),
        )

    def test_a_missing_or_empty_watermark_is_none_and_garbage_raises(self):
        for payload in ({}, {"watermark": None}, {"watermark": "  "}, None, "not a dict"):
            self.assertIsNone(tiers.parse_chain_watermark(payload), payload)
        with self.assertRaises(ValueError):
            tiers.parse_chain_watermark({"watermark": "yesterday"})

    def test_the_state_path_is_the_layout_the_backup_module_writes(self):
        path = tiers.chain_state_path(r"G:\StockPlatform\backups", "quant.raw_market_observations")
        self.assertEqual(
            path, Path(r"G:\StockPlatform\backups") / "incremental" / "quant.raw_market_observations"
            / "state.json"
        )
        self.assertIn("incremental", str(tiers.chain_state_path(None, "quant.a")))
        self.assertTrue(str(tiers.chain_state_path(None, "quant.a")).startswith(tiers.DEFAULT_BACKUP_ROOT))

    def test_a_readable_state_file_yields_its_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = tiers.chain_state_path(directory, "quant.raw_market_observations")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"table": "quant.raw_market_observations", '
                            '"watermark": "2026-09-18T12:00:03.098279Z"}', encoding="utf-8")
            state = tiers.read_chain_state(directory, "quant.raw_market_observations")
        self.assertEqual(state["watermark"], datetime(2026, 9, 18, 12, 0, 3, 98279, tzinfo=timezone.utc))
        self.assertIsNone(state["reason"])

    def test_every_unreadable_state_is_the_same_answer_no_watermark_and_a_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = tiers.read_chain_state(directory, "quant.raw_market_observations")
            broken = tiers.chain_state_path(directory, "quant.b")
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text("{not json", encoding="utf-8")
            unreadable = tiers.read_chain_state(directory, "quant.b")
            empty = tiers.chain_state_path(directory, "quant.c")
            empty.parent.mkdir(parents=True, exist_ok=True)
            empty.write_text('{"table": "quant.c"}', encoding="utf-8")
            no_watermark = tiers.read_chain_state(directory, "quant.c")
        for state, fragment in ((missing, "never completed"), (unreadable, "unreadable"),
                                (no_watermark, "no watermark")):
            self.assertIsNone(state["watermark"])
            self.assertIn(fragment, state["reason"])

    def test_resolve_settings_reads_the_backup_root_and_the_incremental_tables(self):
        args = tiers.build_parser().parse_args(["apply"])
        settings = tiers.resolve_settings(args, {"STOCK_BACKUP_ROOT": r"D:\b"})
        self.assertEqual(settings["backup_root"], r"D:\b")
        self.assertEqual(
            settings["incremental_specs"], {"quant.raw_market_observations": ("created_at", "updated_at")}
        )
        args = tiers.build_parser().parse_args(["apply", "--backup-root", r"E:\override"])
        self.assertEqual(
            tiers.resolve_settings(args, {"STOCK_BACKUP_ROOT": r"D:\b"})["backup_root"], r"E:\override"
        )
        self.assertEqual(
            tiers.resolve_settings(args, {"STOCK_BACKUP_INCREMENTAL_TABLES": "none"})["incremental_specs"],
            {},
        )


class ChainGuardTest(unittest.TestCase):
    """_chain_guard decides, per table, between clamp, pass-through and refusal."""

    POLICY = tiers.TIER_POLICY[0]  # quant.raw_market_observations
    COLUMNS = ["observation_id", "available_at", "created_at", "updated_at"]

    @staticmethod
    def _settings(root, specs=None):
        return {
            "backup_root": str(root),
            "incremental_specs": tiers.parse_incremental_specs(specs),
        }

    def test_a_table_without_a_chunk_chain_moves_unclamped(self):
        result, record = {}, {"errors": []}
        chain = tiers._chain_guard(
            tiers.TIER_POLICY[2], ["observation_id"], self._settings("x"), result, record
        )
        self.assertEqual(chain, ((), {}))
        self.assertFalse(result["chain_protected"])
        self.assertEqual(record["errors"], [])

    def test_a_chained_table_is_clamped_to_its_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            path = tiers.chain_state_path(directory, self.POLICY.qualified)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"watermark": "2026-09-18T12:00:03.098279Z"}', encoding="utf-8")
            result, record = {}, {"errors": []}
            columns, params = tiers._chain_guard(
                self.POLICY, self.COLUMNS, self._settings(directory), result, record
            )
        self.assertEqual(columns, ("created_at", "updated_at"))
        self.assertEqual(params["chain_watermark"], datetime(2026, 9, 18, 12, 0, 3, 98279, tzinfo=timezone.utc))
        self.assertTrue(result["chain_protected"])
        self.assertEqual(result["chain_watermark"], "2026-09-18T12:00:03.098279+00:00")

    def test_a_missing_state_file_moves_nothing_and_says_why(self):
        with tempfile.TemporaryDirectory() as directory:
            result, record = {}, {"errors": []}
            chain = tiers._chain_guard(
                self.POLICY, self.COLUMNS, self._settings(directory), result, record
            )
        self.assertIsNone(chain, "no watermark must mean no row leaves the hot table")
        self.assertEqual(result["status"], "chain_missing")
        self.assertIn("never completed", result["detail"])
        self.assertEqual(len(record["errors"]), 1)

    def test_a_spec_naming_a_column_the_table_lacks_refuses_rather_than_moving_unclamped(self):
        with tempfile.TemporaryDirectory() as directory:
            result, record = {}, {"errors": []}
            chain = tiers._chain_guard(
                self.POLICY, ["observation_id", "available_at"], self._settings(directory), result, record
            )
        self.assertIsNone(chain)
        self.assertEqual(result["status"], "chain_columns_missing")
        self.assertIn("created_at", result["detail"])


class ReadOnlyStatusTest(unittest.TestCase):
    """plan/status must not answer 'ok' while install has never run."""

    @staticmethod
    def _report(space="ok", tablespace=True, quarantine=True, tables=()):
        return {
            "space_policy": {"status": space},
            "tablespace_installed": tablespace,
            "quarantine_table_installed": quarantine,
            "tables": list(tables),
        }

    def test_a_fully_installed_tier_within_budget_is_ok(self):
        report = self._report(tables=[{"exists": True, "cold_exists": True}])
        self.assertEqual(tiers._read_only_status(report), "ok")

    def test_a_missing_tablespace_or_quarantine_table_is_degraded(self):
        for kwargs in ({"tablespace": False}, {"quarantine": False}):
            report = self._report(**kwargs)
            self.assertEqual(tiers._read_only_status(report), "degraded", kwargs)
            self.assertEqual(tiers.exit_code_for("degraded"), 2)
            self.assertIn("install has not run", report["reason"])

    def test_a_hot_table_without_its_twin_is_degraded(self):
        report = self._report(tables=[{"exists": True, "cold_exists": False}])
        self.assertEqual(tiers._read_only_status(report), "degraded")
        self.assertIn("cold twin", report["reason"])

    def test_an_unmeasurable_directory_still_outranks_everything(self):
        for space in sorted(tiers.DEGRADED_SPACE_STATUSES):
            self.assertEqual(tiers._read_only_status(self._report(space=space)), "degraded", space)


@contextmanager
def _temp_tree():
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory)


def _write(path: Path, size: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _make_junction(link: Path, target: Path) -> bool:
    """A real NTFS directory junction, the way PostgreSQL makes pg_tblspc entries."""
    if os.name != "nt":
        return False
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and link.exists()


class DirectorySizeTest(unittest.TestCase):
    """The 500 GB cap is only as good as the number it is measured against."""

    def test_a_plain_tree_is_summed_recursively(self):
        with _temp_tree() as root:
            _write(root / "base" / "1.dat", 1000)
            _write(root / "base" / "16384" / "2.dat", 2000)
            _write(root / "PG_VERSION", 3)
            self.assertEqual(tiers.directory_size_bytes(root), 3003)

    def test_a_missing_or_empty_path_is_unmeasurable_not_zero(self):
        with _temp_tree() as root:
            self.assertIsNone(tiers.directory_size_bytes(root / "nope"))
            self.assertIsNone(tiers.directory_size_bytes(None))
            self.assertIsNone(tiers.directory_size_bytes(""))

    @unittest.skipUnless(os.name == "nt", "directory junctions are an NTFS concept")
    def test_a_junction_is_neither_followed_nor_counted(self):
        # This is the review's blocker: PGDATA\pg_tblspc\<oid> is a junction to
        # G:\StockPlatform\data\pg-cold.  A walk that descends it reports the
        # whole cold tier as hot usage, and the space policy then starts taking
        # history away to "fix" bytes that are not on the hot volume at all.
        with _temp_tree() as root:
            pgdata = root / "pgdata"
            cold = root / "cold"
            _write(pgdata / "base" / "1.dat", 1000)
            _write(cold / "PG_16_202307071" / "big.dat", 500_000)
            (pgdata / "pg_tblspc").mkdir(parents=True, exist_ok=True)
            if not _make_junction(pgdata / "pg_tblspc" / "16385", cold):
                self.skipTest("mklink /J is not available in this environment")
            skipped: list[str] = []
            measured = tiers.directory_size_bytes(pgdata, collect_reparse_points=skipped)
            self.assertEqual(measured, 1000, "the cold tier behind the junction must not be counted")
            self.assertEqual(skipped, [os.path.join("pg_tblspc", "16385")])
            # And the junction target itself is of course still measurable.
            self.assertEqual(tiers.directory_size_bytes(cold), 500_000)

    @unittest.skipUnless(os.name == "nt", "directory junctions are an NTFS concept")
    def test_measure_pgdata_reports_wal_separately_and_names_the_junction(self):
        with _temp_tree() as root:
            pgdata = root / "pgdata"
            cold = root / "cold"
            _write(pgdata / "base" / "1.dat", 1000)
            _write(pgdata / "pg_wal" / "000000010000000000000001", 4000)
            _write(cold / "PG_16_202307071" / "big.dat", 500_000)
            (pgdata / "pg_tblspc").mkdir(parents=True, exist_ok=True)
            if not _make_junction(pgdata / "pg_tblspc" / "16385", cold):
                self.skipTest("mklink /J is not available in this environment")
            report = tiers.measure_pgdata(pgdata)
        self.assertTrue(report["measured"])
        self.assertEqual(report["usage_bytes"], 5000)
        self.assertEqual(report["wal_bytes"], 4000)
        # A WAL spike is transient and no amount of tiering shrinks it.
        self.assertEqual(report["tiering_usage_bytes"], 1000)
        self.assertEqual(report["excluded_reparse_points"], [os.path.join("pg_tblspc", "16385")])
        self.assertGreater(report["volume_total_bytes"], 0)

    def test_measure_pgdata_on_an_unreadable_directory_says_unmeasured(self):
        report = tiers.measure_pgdata(r"Z:\definitely\not\here")
        self.assertFalse(report["measured"])
        self.assertIsNone(report["usage_bytes"])
        self.assertIsNone(report["tiering_usage_bytes"])


class SpaceVerdictTest(unittest.TestCase):
    """space_verdict is the bridge between the measurement and the pure policy."""

    @staticmethod
    def _usage(**overrides):
        usage = {
            "measured": True,
            "pgdata_dir": r"F:\StockPlatformDB\postgresql16",
            "usage_bytes": 100 * GB,
            "wal_bytes": 10 * GB,
            "tiering_usage_bytes": 90 * GB,
            "budget_bytes": 100 * GB,
            "configured_budget_bytes": 500 * GB,
            "volume_capacity_bytes": 100 * GB,
            "budget_fits_volume": True,
            "excluded_reparse_points": [],
        }
        usage.update(overrides)
        return usage

    STATS = {
        "quant.a": {"exists": True, "size_bytes": 40 * GB, "oldest_hot_days": 400},
    }

    def test_an_unmeasurable_directory_alerts_and_degrades(self):
        verdict = tiers.space_verdict(self._usage(measured=False), self.STATS)
        self.assertEqual(verdict["status"], "unknown")
        self.assertTrue(verdict["alert"], "an unenforced cap must never look like a green night")
        self.assertIn(verdict["status"], tiers.DEGRADED_SPACE_STATUSES)

    def test_wal_bytes_are_excluded_from_the_tiering_decision(self):
        # 100 GB on disk against a 100 GB budget is 100 %, but 10 GB of that is
        # WAL: the tiering decision sees 90 % and acts on that.
        verdict = tiers.space_verdict(self._usage(), self.STATS)
        self.assertEqual(verdict["usage_bytes"], 90 * GB)
        self.assertEqual(verdict["wal_bytes"], 10 * GB)

    def test_a_budget_the_volume_cannot_hold_alerts(self):
        verdict = tiers.space_verdict(
            self._usage(usage_bytes=10 * GB, tiering_usage_bytes=10 * GB, budget_fits_volume=False),
            self.STATS,
        )
        self.assertTrue(verdict["alert"])
        self.assertTrue(verdict["budget_exceeds_volume"])
        self.assertIn("does not fit the volume", verdict["reason"])

    def test_the_excluded_junctions_are_carried_into_the_receipt(self):
        verdict = tiers.space_verdict(
            self._usage(usage_bytes=10 * GB, tiering_usage_bytes=10 * GB,
                        excluded_reparse_points=["pg_tblspc\\16385"]),
            self.STATS,
        )
        self.assertEqual(verdict["excluded_reparse_points"], ["pg_tblspc\\16385"])


class MoveStatementShapeTest(unittest.TestCase):
    """The move is insert-first-then-delete-by-PK with explicit column lists.

    ``psycopg.sql`` objects are rendered without a connection here.  That pins
    the *shape* the safety argument depends on and nothing more: no statement in
    this class is ever executed, and no row is ever moved.  The behaviour is
    executed by ``ScratchDatabaseMoveTest`` (opt-in, see the module docstring)
    and by the branch's end-to-end script against a scratch database.
    """

    POLICY = tiers.TIER_POLICY[2]  # quant.intraday_quote_observations
    COLUMNS = ["observation_id", "symbol", "source_name", "observed_at", "payload"]
    PK = ["observation_id"]
    KEYS = [("observation_id",), ("symbol", "source_name", "observed_at")]

    @staticmethod
    def _render(statement) -> str:
        return statement.as_string(None)

    def test_the_snapshot_names_every_column_and_never_selects_star(self):
        rendered = self._render(tiers.snapshot_batch_sql(self.POLICY, self.COLUMNS))
        self.assertIn("CREATE TEMPORARY TABLE tier_batch ON COMMIT DROP", rendered)
        self.assertIn("ON COMMIT DROP", rendered, "a crashed batch must leave no temp table behind")
        for column in self.COLUMNS:
            self.assertIn(f'"{column}"', rendered)
        self.assertNotIn("SELECT *", rendered)
        self.assertIn('"observed_at" < %(cutoff)s', rendered)
        self.assertIn("LIMIT %(batch)s", rendered)

    def test_the_snapshot_locks_the_rows_it_is_about_to_delete(self):
        # Without FOR UPDATE an UPDATE committed between the snapshot and the
        # delete-by-PK is lost: the twin keeps the pre-update version and the
        # DELETE removes the newer one.  These tables have upsert paths.
        rendered = self._render(tiers.snapshot_batch_sql(self.POLICY, self.COLUMNS))
        self.assertTrue(rendered.rstrip().endswith("FOR UPDATE"), rendered[-80:])
        self.assertLess(rendered.index("LIMIT %(batch)s"), rendered.index("FOR UPDATE"))

    def test_the_chain_clamp_is_off_unless_the_table_has_a_chunk_chain(self):
        self.assertNotIn("chain_watermark", self._render(tiers.snapshot_batch_sql(self.POLICY, self.COLUMNS)))

    def test_the_chain_clamp_withholds_rows_the_backup_chain_has_not_exported(self):
        rendered = self._render(
            tiers.snapshot_batch_sql(self.POLICY, self.COLUMNS, ("created_at", "updated_at"))
        )
        # NULL never reaches the watermark, so a row with no update stamp is not
        # withheld by that column.
        self.assertIn('("created_at" IS NULL OR "created_at" < %(chain_watermark)s)', rendered)
        self.assertIn('("updated_at" IS NULL OR "updated_at" < %(chain_watermark)s)', rendered)
        self.assertLess(rendered.index("chain_watermark"), rendered.index("ORDER BY"))
        self.assertTrue(rendered.rstrip().endswith("FOR UPDATE"))

    def test_the_withheld_probe_is_the_exact_negation_and_is_bounded(self):
        rendered = self._render(tiers.withheld_by_chain_sql(self.POLICY, ("created_at",)))
        self.assertIn('NOT (TRUE AND ("created_at" IS NULL OR "created_at" < %(chain_watermark)s))', rendered)
        self.assertIn('"observed_at" < %(cutoff)s', rendered)
        self.assertIn("LIMIT %(probe)s", rendered)
        self.assertEqual(tiers.CHAIN_PROBE_ROWS, 100_000)

    def test_the_conflict_scan_covers_every_unique_key_and_compares_whole_rows(self):
        rendered = self._render(tiers.conflict_scan_sql(self.POLICY, self.PK, self.KEYS))
        self.assertIn("to_jsonb(b.*) IS DISTINCT FROM to_jsonb(c.*)", rendered)
        self.assertEqual(rendered.count("UNION ALL"), len(self.KEYS) - 1)
        self.assertIn('"b"."symbol" = "c"."symbol"', rendered)
        self.assertIn('"b"."source_name" = "c"."source_name"', rendered)
        # `=` and not IS NOT DISTINCT FROM: a NULL never collides in a unique
        # index, so a NULL-matching join would quarantine innocent rows.
        self.assertNotIn("IS NOT DISTINCT FROM", rendered)

    def test_the_quarantine_insert_keeps_both_rows_and_the_hot_primary_key(self):
        rendered = self._render(tiers.quarantine_sql(self.PK))
        self.assertIn('INSERT INTO "quant"."storage_tier_conflicts"', rendered)
        self.assertIn("(table_name, hot_pk, hot_row, cold_row)", rendered)
        self.assertIn("jsonb_build_object('observation_id', q.\"observation_id\")", rendered)
        self.assertIn("q.hot_row, q.cold_row", rendered)

    def test_the_insert_names_its_columns_and_skips_the_quarantined_rows(self):
        rendered = self._render(tiers.insert_batch_sql(self.POLICY, self.COLUMNS, self.PK))
        self.assertIn('INSERT INTO "quant"."intraday_quote_observations_cold" (', rendered)
        for column in self.COLUMNS:
            self.assertIn(f'"{column}"', rendered)
        self.assertNotIn("SELECT *", rendered)
        self.assertIn("NOT EXISTS (SELECT 1 FROM tier_conflicts q", rendered)
        # ON CONFLICT DO NOTHING is now only reachable for a byte-identical row:
        # anything that differs was routed to quarantine one statement earlier.
        self.assertIn("ON CONFLICT DO NOTHING", rendered)

    def test_the_delete_is_by_primary_key_against_the_snapshot(self):
        rendered = self._render(tiers.delete_batch_sql(self.POLICY, self.PK))
        self.assertIn('DELETE FROM "quant"."intraday_quote_observations" h USING tier_batch b', rendered)
        self.assertIn('"h"."observation_id" = "b"."observation_id"', rendered)
        self.assertNotIn("ctid", rendered, "ctid is not stable across a statement boundary")

    def test_no_statement_deletes_before_the_twin_holds_the_rows(self):
        # The ordering is the whole argument: snapshot, quarantine, insert,
        # delete -- all in one transaction, so an interruption rolls back.
        order = [
            tiers.snapshot_batch_sql(self.POLICY, self.COLUMNS),
            tiers.conflict_scan_sql(self.POLICY, self.PK, self.KEYS),
            tiers.quarantine_sql(self.PK),
            tiers.insert_batch_sql(self.POLICY, self.COLUMNS, self.PK),
            tiers.delete_batch_sql(self.POLICY, self.PK),
        ]
        rendered = [self._render(statement) for statement in order]
        self.assertTrue(rendered[0].startswith("CREATE TEMPORARY TABLE"))
        self.assertTrue(rendered[3].startswith("INSERT INTO"))
        self.assertTrue(rendered[4].startswith("DELETE FROM"))
        for statement in rendered[:4]:
            self.assertNotIn("DELETE FROM \"quant\".\"intraday_quote_observations\" ", statement)


class TimeoutsTest(unittest.TestCase):
    """install takes AccessExclusiveLock on gigabytes; it must never queue."""

    def test_every_connection_carries_a_lock_timeout(self):
        captured = {}

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return FakeConnection()

        fake_psycopg = SimpleNamespace(connect=fake_connect)
        with patch.dict(sys.modules, {"psycopg": fake_psycopg}):
            tiers.connect({"PGHOST": "h", "PGPORT": "1", "PGDATABASE": "d", "PGUSER": "u", "PGPASSWORD": ""})
        self.assertIn(f"-c lock_timeout={tiers.LOCK_TIMEOUT_MS}", captured["options"])

    def test_the_read_only_commands_are_read_only_on_the_server(self):
        captured = {}

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return FakeConnection()

        with patch.dict(sys.modules, {"psycopg": SimpleNamespace(connect=fake_connect)}):
            tiers.connect({}, read_only=True, statement_timeout_ms=1234)
        self.assertIn("-c default_transaction_read_only=on", captured["options"])
        self.assertIn("-c statement_timeout=1234", captured["options"])

    def test_the_install_and_index_timeouts_are_sized_for_the_real_tables(self):
        # 2.6 GB tablespace rewrite and a CONCURRENTLY build on a 19 GB table.
        self.assertGreaterEqual(tiers.INSTALL_STATEMENT_TIMEOUT_MS, 30 * 60 * 1000)
        self.assertGreaterEqual(tiers.INDEX_STATEMENT_TIMEOUT_MS, tiers.INSTALL_STATEMENT_TIMEOUT_MS)
        self.assertEqual(tiers.LOCK_TIMEOUT_MS, 30_000)

    def test_the_vacuum_gets_its_own_ceiling_well_above_the_batch_timeout(self):
        # The first drain of a 19 GB table with six indexes vacuums for longer
        # than one 20 000-row batch is allowed to take; under the batch timeout
        # that turned a completely successful move into 'failed' for the table.
        self.assertEqual(tiers.VACUUM_STATEMENT_TIMEOUT_MS, 30 * 60 * 1000)
        self.assertGreater(tiers.VACUUM_STATEMENT_TIMEOUT_MS, tiers.DEFAULT_STATEMENT_TIMEOUT_MS)


class VacuumIsolationTest(unittest.TestCase):
    """A vacuum that overruns must not fail a move whose rows are already moved."""

    POLICY = tiers.TIER_POLICY[0]

    class _Conn:
        def __init__(self, fail=None):
            self.statements: list[str] = []
            self.fail = fail

        def execute(self, statement, params=None):
            text = statement if isinstance(statement, str) else statement.as_string(None)
            self.statements.append(text)
            if self.fail is not None and "VACUUM" in text:
                raise self.fail
            return self

    SETTINGS = {"statement_timeout_ms": tiers.DEFAULT_STATEMENT_TIMEOUT_MS}

    def test_nothing_is_vacuumed_when_nothing_moved(self):
        conn = self._Conn()
        result = {"deleted_rows": 0}
        tiers._vacuum_after_move(conn, self.POLICY, self.SETTINGS, result)
        self.assertEqual(conn.statements, [])
        self.assertNotIn("vacuum", result)

    def test_the_vacuum_raises_the_timeout_and_puts_it_back(self):
        conn = self._Conn()
        result = {"deleted_rows": 5}
        tiers._vacuum_after_move(conn, self.POLICY, self.SETTINGS, result)
        self.assertEqual(result["vacuum"], "ok")
        self.assertTrue(result["vacuumed"])
        self.assertIn(f"SET \"statement_timeout\" = '{tiers.VACUUM_STATEMENT_TIMEOUT_MS}ms'", conn.statements[0])
        self.assertIn("VACUUM (ANALYZE)", conn.statements[1])
        self.assertIn(f"'{tiers.DEFAULT_STATEMENT_TIMEOUT_MS}ms'", conn.statements[2])

    def test_a_timed_out_vacuum_is_recorded_not_raised(self):
        import psycopg

        conn = self._Conn(fail=psycopg.errors.QueryCanceled("canceling statement"))
        result = {"deleted_rows": 5, "status": "ok"}
        tiers._vacuum_after_move(conn, self.POLICY, self.SETTINGS, result)
        self.assertEqual(result["vacuum"], "timed_out")
        self.assertFalse(result["vacuumed"])
        self.assertEqual(result["status"], "ok", "the move's correctness does not depend on the vacuum")
        self.assertIn(f"'{tiers.DEFAULT_STATEMENT_TIMEOUT_MS}ms'", conn.statements[-1])

    def test_any_other_vacuum_failure_is_recorded_too(self):
        conn = self._Conn(fail=RuntimeError("disk full"))
        result = {"deleted_rows": 5, "status": "ok"}
        tiers._vacuum_after_move(conn, self.POLICY, self.SETTINGS, result)
        self.assertEqual(result["vacuum"], "failed")
        self.assertIn("disk full", result["vacuum_error"])
        self.assertEqual(result["status"], "ok")


class _Rows:
    """The slice of a psycopg cursor these code paths actually use."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class CutoffIndexRepairTest(unittest.TestCase):
    """Repairing a cancelled CREATE INDEX CONCURRENTLY, by statement shape.

    ``IF NOT EXISTS`` matches an INVALID index by name and does nothing, so the
    repair has to drop it first -- and CONCURRENTLY, because the plain form takes
    an AccessExclusiveLock on a 19 GB table in the middle of the night.  Nothing
    here is executed: the fake connection records the statement text and answers
    the catalog reads from a scripted list of ``cutoff_index_state`` rows.
    """

    POLICY = tiers.TIER_POLICY[0]  # quant.raw_market_observations

    class _Conn:
        def __init__(self, states, *, lock_refuses=()):
            self.statements: list[str] = []
            self._states = list(states)
            self._lock_refuses = tuple(lock_refuses)

        def execute(self, statement, params=None):
            import psycopg

            text = statement if isinstance(statement, str) else statement.as_string(None)
            self.statements.append(text)
            for token in self._lock_refuses:
                if token in text:
                    raise psycopg.errors.LockNotAvailable(f"could not obtain lock for {token}")
            if "to_regclass" in text:
                return _Rows([("quant.raw_market_observations",)])
            if "i.indisvalid, i.indisready" in text:
                state = self._states.pop(0) if self._states else None
                return _Rows([] if state is None else [state])
            if "SELECT EXISTS" in text:
                return _Rows([(False,)])
            return _Rows([])

    @staticmethod
    def _notes():
        actions: list[dict] = []

        def note(action, target, result, **extra):
            actions.append({"action": action, "target": target, "result": result, **extra})

        return actions, note

    def _run(self, states, **kwargs):
        conn = self._Conn(states, **kwargs)
        actions, note = self._notes()
        tiers._install_cutoff_index(conn, self.POLICY, note)
        return conn, actions

    @staticmethod
    def _index_results(actions):
        return [a["result"] for a in actions if a["action"] == "create_cutoff_index"]

    def test_an_invalid_index_is_dropped_concurrently_before_it_is_rebuilt(self):
        # exists-but-invalid, then a valid index after the rebuild.
        conn, actions = self._run([(False, True), (True, True)])
        drops = [s for s in conn.statements if "DROP INDEX" in s]
        creates = [s for s in conn.statements if "CREATE INDEX" in s]
        self.assertEqual(len(drops), 1, conn.statements)
        self.assertEqual(len(creates), 1, conn.statements)
        self.assertIn("DROP INDEX CONCURRENTLY IF EXISTS", drops[0])
        self.assertIn(f'"{self.POLICY.cutoff_index}"', drops[0])
        self.assertIn("CREATE INDEX CONCURRENTLY IF NOT EXISTS", creates[0])
        self.assertIn(f'"{self.POLICY.column}"', creates[0])
        # The order is the whole point: IF NOT EXISTS matches the invalid index
        # by name, so a CREATE issued first would silently do nothing.
        self.assertLess(
            conn.statements.index(drops[0]),
            conn.statements.index(creates[0]),
            "the invalid index must be dropped before the rebuild is attempted",
        )
        # CONCURRENTLY on both sides: a plain DROP INDEX takes an
        # AccessExclusiveLock on the hot table the platform is still reading.
        self.assertNotIn("DROP INDEX IF EXISTS", drops[0])
        self.assertEqual(self._index_results(actions), ["rebuilt_invalid"])

    def test_a_drop_that_cannot_take_its_lock_never_claims_a_rebuild(self):
        conn, actions = self._run([(False, True)], lock_refuses=("DROP INDEX",))
        self.assertEqual(
            [s for s in conn.statements if "CREATE INDEX" in s],
            [],
            "nothing may be built while the invalid index still holds the name",
        )
        self.assertEqual(self._index_results(actions), ["invalid_index_present"])
        self.assertIn("re-run install", actions[-1]["detail"])
        # _lock_guarded records the refusal itself, so install finishes 'partial'.
        self.assertIn("skipped_locked", [a["result"] for a in actions])

    def test_a_rebuild_that_lands_invalid_again_is_not_reported_as_repaired(self):
        # The second pass of a CONCURRENTLY build can fail without raising, and
        # the readback is the only way to tell.  Reporting 'rebuilt_invalid'
        # here would tell the operator the repair worked while the table still
        # has no usable cutoff index.
        conn, actions = self._run([(False, True), (False, True)])
        self.assertEqual(len(conn.statements), 5, conn.statements)
        self.assertEqual(self._index_results(actions), ["invalid_index_present"])
        self.assertNotIn("rebuilt_invalid", self._index_results(actions))
        self.assertIn("the next install", actions[-1]["detail"])

    def test_the_readback_is_a_second_state_query_after_the_create(self):
        conn, _ = self._run([(False, True), (True, True)])
        states = [i for i, s in enumerate(conn.statements) if "i.indisvalid, i.indisready" in s]
        create = next(i for i, s in enumerate(conn.statements) if "CREATE INDEX" in s)
        self.assertEqual(len(states), 2, conn.statements)
        self.assertLess(states[0], create)
        self.assertLess(create, states[1], "the claim must be read back, not assumed")


class _MoveConn:
    """A connection just real enough to walk ``_move_table`` to its chain probe.

    Every statement is recorded as text and answered from fixed rows; the batch
    loop is handed an empty snapshot, so one pass reaches the probe with nothing
    moved.  No statement is executed and no row exists.
    """

    COLUMNS = [
        ("observation_id", "bigint"),
        ("available_at", "timestamp with time zone"),
        ("created_at", "timestamp with time zone"),
        ("updated_at", "timestamp with time zone"),
    ]

    def __init__(self, *, unsupported=(), probe_error=None, probe_rows=0):
        self.statements: list[str] = []
        self.unsupported = list(unsupported)
        self.probe_error = probe_error
        self.probe_rows = probe_rows

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, statement, params=None):
        text = statement if isinstance(statement, str) else statement.as_string(None)
        self.statements.append(text)
        if "%(probe)s" in text:
            if self.probe_error is not None:
                raise self.probe_error
            return _Rows([(self.probe_rows,)])
        if "to_regclass" in text:
            return _Rows([("present",)])
        if "format_type" in text:
            return _Rows(list(self.COLUMNS))
        if "indpred IS NOT NULL" in text:
            return _Rows([(name,) for name in self.unsupported])
        if "indnkeyatts" in text:
            return _Rows([("pk", True, ["observation_id"], 1)])
        if "indisprimary" in text:
            return _Rows([("observation_id",)])
        if "FROM tier_batch" in text:
            return _Rows([(0,)])
        return _Rows([])


class MoveRefusalAndProbeTest(unittest.TestCase):
    """The two ways _move_table stops short, driven through a fake connection."""

    POLICY = tiers.TIER_POLICY[0]  # quant.raw_market_observations
    NOW = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)

    @contextmanager
    def _settings(self, *, chain=True):
        with tempfile.TemporaryDirectory() as directory:
            if chain:
                path = tiers.chain_state_path(directory, self.POLICY.qualified)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"watermark": "2026-09-19T03:40:00Z"}', encoding="utf-8")
            yield {
                "backup_root": directory,
                "incremental_specs": tiers.parse_incremental_specs(None),
                "statement_timeout_ms": tiers.DEFAULT_STATEMENT_TIMEOUT_MS,
                "batch_rows": 1000,
                "max_batches": 0,
            }

    def _move(self, conn, settings):
        record = {"errors": []}
        result = tiers._move_table(
            conn, self.POLICY, self.NOW, self.POLICY.hot_days, settings, record
        )
        return result, record

    # --- a partial or expression unique index refuses the table -------------

    def test_the_unsupported_index_probe_asks_for_valid_partial_or_expression_uniques(self):
        conn = _MoveConn(unsupported=["quant.raw_obs_recent_uq"])
        tiers.unsupported_unique_indexes(conn, self.POLICY.qualified)
        probe = conn.statements[-1]
        self.assertIn("i.indisunique", probe)
        # An INVALID index enforces nothing, so it cannot swallow a row; asking
        # about it would refuse tables that are perfectly movable.
        self.assertIn("i.indisvalid", probe)
        self.assertIn("(i.indpred IS NOT NULL OR i.indexprs IS NOT NULL)", probe)
        self.assertNotIn("indpred IS NULL", probe, "that is the other query, the one for usable keys")

    def test_a_partial_or_expression_unique_index_refuses_the_move_and_is_named(self):
        conn = _MoveConn(unsupported=["quant.raw_obs_recent_uq"])
        with self._settings() as settings:
            result, record = self._move(conn, settings)
        self.assertEqual(result["status"], "unsupported_unique_index")
        self.assertEqual(result["unsupported_unique_indexes"], ["quant.raw_obs_recent_uq"])
        self.assertIn("conflict scan cannot join on it", result["detail"])
        self.assertEqual(len(record["errors"]), 1, record["errors"])
        self.assertEqual(record["errors"][0]["table"], self.POLICY.qualified)
        self.assertIn("quant.raw_obs_recent_uq", record["errors"][0]["error"])
        # The refusal is before any row moves: no snapshot, no delete.
        self.assertEqual([s for s in conn.statements if "tier_batch" in s], [])
        self.assertEqual(result["deleted_rows"], 0)

    # --- the chain probe ----------------------------------------------------

    def test_a_probe_that_cannot_be_answered_reports_the_clamp_as_unknown(self):
        import psycopg

        conn = _MoveConn(probe_error=psycopg.errors.QueryCanceled("canceling statement due to timeout"))
        with self._settings() as settings:
            result, record = self._move(conn, settings)
        # _scalar would have swallowed this to None, which reads exactly like
        # "nothing withheld" -- the opposite finding.
        self.assertEqual(result["status"], "chain_behind")
        self.assertIn("chain_withheld_rows", result)
        self.assertIsNone(result["chain_withheld_rows"])
        self.assertIn("unknown, not zero", result["detail"])
        self.assertIn("QueryCanceled", result["detail"])
        # A probe is not a move failure: the rows that did move are fine.
        self.assertEqual(record["errors"], [])
        self.assertNotIn("error", result)

    def test_a_probe_that_answers_zero_leaves_the_run_ok(self):
        conn = _MoveConn(probe_rows=0)
        with self._settings() as settings:
            result, _ = self._move(conn, settings)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("chain_withheld_rows", result)

    def test_a_probe_that_finds_withheld_rows_reports_chain_behind_with_the_count(self):
        conn = _MoveConn(probe_rows=42)
        with self._settings() as settings:
            result, _ = self._move(conn, settings)
        self.assertEqual(result["status"], "chain_behind")
        self.assertEqual(result["chain_withheld_rows"], 42)
        self.assertFalse(result["chain_withheld_rows_capped"])


def _migration_statements(function):
    statements: list[str] = []
    autocommit: list[str] = []
    state = {"autocommit": False}

    @contextmanager
    def autocommit_block():
        state["autocommit"] = True
        try:
            yield
        finally:
            state["autocommit"] = False

    def execute(sql, *args, **kwargs):
        text = " ".join(sql.split())
        statements.append(text)
        if state["autocommit"]:
            autocommit.append(text)

    with patch.object(alembic.op, "execute", execute), \
            patch.object(alembic.op, "get_context", lambda: SimpleNamespace(autocommit_block=autocommit_block)):
        function()
    return statements, autocommit


class CutoffIndexMigrationTest(unittest.TestCase):
    """install and a rebuilt database must create the same five indexes.

    Without an index leading with the cutoff column every 20 000-row batch
    re-scans and re-sorts a whole index; on the 19 GB raw_market_observations
    that is a full pass per batch.
    """

    @classmethod
    def setUpClass(cls):
        cls.module = _load_migration(MIGRATIONS / "20260919_0106_storage_tier_cutoff_indexes.py")

    def test_the_migration_covers_exactly_the_tier_policy(self):
        self.assertEqual(
            [(name, table, column) for name, table, column in self.module.INDEXES],
            [(policy.cutoff_index, policy.qualified, policy.column) for policy in tiers.TIER_POLICY],
            "the migration and TIER_POLICY in scripts/database-storage-tiers.py have drifted apart",
        )

    def test_every_index_is_created_concurrently_inside_autocommit(self):
        statements, autocommit = _migration_statements(self.module.upgrade)
        self.assertEqual(statements, autocommit)
        self.assertEqual(statements, [
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS raw_market_observations_tier_cutoff_idx "
            "ON quant.raw_market_observations (available_at)",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS tushare_raw_records_tier_cutoff_idx "
            "ON quant.tushare_raw_records (available_at)",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS intraday_quote_observations_tier_cutoff_idx "
            "ON quant.intraday_quote_observations (observed_at)",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS intraday_rule_input_snapshots_tier_cutoff_idx "
            "ON quant.intraday_rule_input_snapshots (observed_at)",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS edge_evidence_changes_tier_cutoff_idx "
            "ON quant.edge_evidence_changes (changed_at)",
        ])

    def test_downgrade_drops_every_index_concurrently(self):
        statements, autocommit = _migration_statements(self.module.downgrade)
        self.assertEqual(statements, autocommit)
        self.assertEqual(
            statements,
            [f"DROP INDEX CONCURRENTLY IF EXISTS quant.{name}" for name, _, _ in reversed(self.module.INDEXES)],
        )

    def test_chain_position(self):
        self.assertEqual(self.module.revision, "20260919_0106")
        self.assertEqual(self.module.down_revision, "20260918_0105")


class CliContractTest(unittest.TestCase):
    """run-storage-tiers.ps1 forwards these flags verbatim; they are a contract."""

    def test_apply_accepts_the_deadline_and_bound_flags(self):
        args = tiers.build_parser().parse_args(
            ["apply", "--env-file", "x.env", "--deadline", "08:00", "--max-seconds", "7200",
             "--max-space-days", "3", "--batch", "500", "--tablespace", "scratch_cold"]
        )
        self.assertEqual(args.command, "apply")
        self.assertEqual(args.deadline, "08:00")
        self.assertEqual(args.max_seconds, 7200)
        self.assertEqual(args.max_space_days, 3)
        self.assertEqual(args.batch, 500)
        self.assertEqual(args.tablespace, "scratch_cold")

    def test_every_command_accepts_the_shared_flags(self):
        for command in ("install", "plan", "apply", "status"):
            args = tiers.build_parser().parse_args([command, "--deadline", "08:00", "--pgdata-dir", "D:\\x"])
            self.assertEqual(args.deadline, "08:00")
            self.assertEqual(args.pgdata_dir, "D:\\x")

    def test_resolve_settings_defaults_the_day_cap_and_reads_the_env(self):
        args = tiers.build_parser().parse_args(["apply"])
        settings = tiers.resolve_settings(args, {"PGDATA_DIR": r"F:\StockPlatformDB\postgresql16"})
        self.assertEqual(settings["max_space_days"], tiers.DEFAULT_MAX_SPACE_DAYS)
        self.assertEqual(settings["pgdata_dir"], r"F:\StockPlatformDB\postgresql16")
        self.assertEqual(settings["budget_bytes"], tiers.DEFAULT_BUDGET_BYTES)


class ReadEnvFileTest(unittest.TestCase):
    def test_comments_quotes_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.env"
            path.write_text(
                "\ufeff# comment\n\nPGDATA_DIR=F:\\StockPlatformDB\\postgresql16\n"
                'PGDATA_BUDGET_BYTES="500GB"\nBROKEN LINE\n',
                encoding="utf-8",
            )
            values = tiers.read_env_file(path)
        self.assertEqual(
            values, {"PGDATA_DIR": "F:\\StockPlatformDB\\postgresql16", "PGDATA_BUDGET_BYTES": "500GB"}
        )


SCRATCH_ENABLED = os.environ.get("STORAGE_TIERS_SCRATCH_TEST") == "1"
SCRATCH_DB = os.environ.get("STORAGE_TIERS_SCRATCH_DB", "trading_hareness_tiers_unittest")


@unittest.skipUnless(
    SCRATCH_ENABLED,
    "set STORAGE_TIERS_SCRATCH_TEST=1 (plus PGHOST/PGPORT/PGADMINUSER/PGADMINPASSWORD) to run the "
    "move against a scratch database it creates and drops itself",
)
class ScratchDatabaseMoveTest(unittest.TestCase):
    """The move, executed -- not rendered -- against a database of its own.

    Everything else in this file asserts statement *shapes*.  This one creates
    ``STORAGE_TIERS_SCRATCH_DB``, builds a hot table, a twin and the quarantine
    table by hand, and runs ``_move_table`` over them, so the three claims the
    design rests on are observed rather than argued:

    * hot + cold + quarantine is conserved, and no primary key is in two halves;
    * a natural-key collision with *different* content lands in quarantine
      instead of being swallowed by ``ON CONFLICT DO NOTHING``;
    * the chain clamp withholds exactly the rows the backup chain has not
      exported, and reports ``chain_behind`` rather than moving them.

    It never touches the production database beyond ``CREATE``/``DROP
    DATABASE``, and it drops what it made in ``tearDownClass``.
    """

    SETUP_SQL = """
    CREATE SCHEMA IF NOT EXISTS quant;
    CREATE TABLE quant.raw_market_observations (
        observation_id bigint PRIMARY KEY,
        symbol text NOT NULL,
        available_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz,
        payload_sha256 text NOT NULL,
        UNIQUE (symbol, payload_sha256)
    );
    CREATE TABLE quant.raw_market_observations_cold
        (LIKE quant.raw_market_observations INCLUDING DEFAULTS INCLUDING INDEXES);
    CREATE TABLE quant.storage_tier_conflicts (
        conflict_id bigserial PRIMARY KEY,
        table_name text NOT NULL,
        hot_pk jsonb NOT NULL,
        hot_row jsonb NOT NULL,
        cold_row jsonb NOT NULL,
        detected_at timestamptz NOT NULL DEFAULT now()
    );
    """

    @classmethod
    def _admin(cls, dbname):
        import psycopg

        return psycopg.connect(
            host=os.environ["PGHOST"],
            port=os.environ["PGPORT"],
            dbname=dbname,
            user=os.environ.get("PGADMINUSER") or os.environ["PGUSER"],
            password=os.environ.get("PGADMINPASSWORD") or os.environ.get("PGPASSWORD", ""),
            autocommit=True,
            application_name="tiers-unittest",
        )

    @classmethod
    def _drop(cls):
        with cls._admin("postgres") as conn:
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", (SCRATCH_DB,)
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')

    @classmethod
    def setUpClass(cls):
        cls._drop()
        with cls._admin("postgres") as conn:
            conn.execute(f'CREATE DATABASE "{SCRATCH_DB}"')
        cls.conn = cls._admin(SCRATCH_DB)
        cls.conn.execute(cls.SETUP_SQL)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls._drop()

    def setUp(self):
        self.conn.execute("TRUNCATE quant.raw_market_observations, "
                          "quant.raw_market_observations_cold, quant.storage_tier_conflicts")
        self.now = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
        self.policy = tiers.TIER_POLICY[0]

    def _seed(self, rows):
        self.conn.cursor().executemany(
            "INSERT INTO quant.raw_market_observations "
            "(observation_id, symbol, available_at, created_at, updated_at, payload_sha256) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            rows,
        )

    def _settings(self, backup_root, specs=None, batch=100):
        return {
            "batch_rows": batch,
            "statement_timeout_ms": 60_000,
            "max_batches": 0,
            "backup_root": str(backup_root),
            "incremental_specs": tiers.parse_incremental_specs(specs),
        }

    def _counts(self):
        def one(sql_text):
            return self.conn.execute(sql_text).fetchone()[0]

        return {
            "hot": one("SELECT count(*) FROM quant.raw_market_observations"),
            "cold": one("SELECT count(*) FROM quant.raw_market_observations_cold"),
            "quarantined": one("SELECT count(*) FROM quant.storage_tier_conflicts"),
        }

    def _no_row_in_both_halves(self):
        both = self.conn.execute(
            "SELECT count(*) FROM quant.raw_market_observations h "
            "JOIN quant.raw_market_observations_cold c USING (observation_id)"
        ).fetchone()[0]
        self.assertEqual(both, 0, "a primary key must never be in the hot table and the twin at once")

    def test_a_move_conserves_every_row_and_leaves_the_window_alone(self):
        old = self.now - timedelta(days=400)
        fresh = self.now - timedelta(days=10)
        self._seed([(i, f"60000{i}", old, old, None, f"sha{i}") for i in range(20)]
                   + [(100 + i, f"70000{i}", fresh, fresh, None, f"fresh{i}") for i in range(5)])
        record = {"errors": []}
        result = tiers._move_table(
            self.conn, self.policy, self.now, 365, self._settings("no-chain", specs="none"), record
        )
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["deleted_rows"], 20)
        self.assertEqual(result["inserted_rows"], 20)
        self.assertEqual(self._counts(), {"hot": 5, "cold": 20, "quarantined": 0})
        self.assertEqual(record["errors"], [])
        self._no_row_in_both_halves()
        self.assertEqual(result["vacuum"], "ok")

    def test_a_natural_key_collision_is_quarantined_whole_not_swallowed(self):
        old = self.now - timedelta(days=400)
        self._seed([(1, "600519", old, old, None, "shaA"), (2, "600520", old, old, None, "shaB")])
        # Same (symbol, payload_sha256) as the hot row, different content: the
        # unique index would make ON CONFLICT DO NOTHING drop the hot row.
        self.conn.execute(
            "INSERT INTO quant.raw_market_observations_cold "
            "(observation_id, symbol, available_at, created_at, updated_at, payload_sha256) "
            "VALUES (99, '600519', %s, %s, NULL, 'shaA')", (old, old),
        )
        record = {"errors": []}
        result = tiers._move_table(
            self.conn, self.policy, self.now, 365, self._settings("no-chain", specs="none"), record
        )
        self.assertEqual(result["status"], "conflicts", result)
        self.assertEqual(result["quarantined_rows"], 1)
        self.assertEqual(self._counts(), {"hot": 0, "cold": 2, "quarantined": 1})
        row = self.conn.execute(
            "SELECT table_name, hot_pk, hot_row, cold_row FROM quant.storage_tier_conflicts"
        ).fetchone()
        self.assertEqual(row[0], "quant.raw_market_observations")
        self.assertEqual(row[1], {"observation_id": 1})
        self.assertNotEqual(row[2]["observation_id"], row[3]["observation_id"])
        self._no_row_in_both_halves()

    def test_the_chain_clamp_withholds_unexported_rows_and_reports_chain_behind(self):
        old = self.now - timedelta(days=400)
        # created_at straddles the watermark: 10 rows are exported, 10 are not,
        # and one of the unexported ones is unexported only via updated_at.
        exported = self.now - timedelta(days=3)
        unexported = self.now - timedelta(hours=1)
        watermark = self.now - timedelta(days=1)
        self._seed(
            [(i, f"60000{i}", old, exported, None, f"sha{i}") for i in range(10)]
            + [(50 + i, f"80000{i}", old, unexported, None, f"late{i}") for i in range(9)]
            + [(90, "800099", old, exported, unexported, "touched")]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = tiers.chain_state_path(directory, self.policy.qualified)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"table": self.policy.qualified,
                                        "watermark": watermark.isoformat().replace("+00:00", "Z")}),
                            encoding="utf-8")
            record = {"errors": []}
            result = tiers._move_table(
                self.conn, self.policy, self.now, 365, self._settings(directory), record
            )
        self.assertEqual(result["status"], "chain_behind", result)
        self.assertTrue(result["chain_protected"])
        self.assertEqual(result["deleted_rows"], 10, "only the exported rows may leave")
        self.assertEqual(result["chain_withheld_rows"], 10)
        self.assertEqual(self._counts(), {"hot": 10, "cold": 10, "quarantined": 0})
        still_hot = self.conn.execute(
            "SELECT count(*) FROM quant.raw_market_observations WHERE created_at >= %s "
            "OR updated_at >= %s", (watermark, watermark)
        ).fetchone()[0]
        self.assertEqual(still_hot, 10, "no row past the watermark may be in the twin")
        self._no_row_in_both_halves()

    def test_a_missing_chain_state_file_moves_nothing_at_all(self):
        old = self.now - timedelta(days=400)
        self._seed([(i, f"60000{i}", old, old, None, f"sha{i}") for i in range(5)])
        with tempfile.TemporaryDirectory() as directory:
            record = {"errors": []}
            result = tiers._move_table(
                self.conn, self.policy, self.now, 365, self._settings(directory), record
            )
        self.assertEqual(result["status"], "chain_missing")
        self.assertEqual(self._counts(), {"hot": 5, "cold": 0, "quarantined": 0})
        self.assertEqual(len(record["errors"]), 1)

    def test_the_snapshot_really_locks_its_rows(self):
        # FOR UPDATE inside CREATE TEMPORARY TABLE AS is the whole point of the
        # lost-update fix, and PostgreSQL has to accept it in that position.
        old = self.now - timedelta(days=400)
        self._seed([(1, "600519", old, old, None, "shaA")])
        statement = tiers.snapshot_batch_sql(
            self.policy,
            ["observation_id", "symbol", "available_at", "created_at", "updated_at", "payload_sha256"],
        )
        with self.conn.transaction():
            self.conn.execute(statement, {"cutoff": self.now - timedelta(days=365), "batch": 10})
            self.assertEqual(self.conn.execute("SELECT count(*) FROM tier_batch").fetchone()[0], 1)
            blocked = self._admin(SCRATCH_DB)
            try:
                blocked.execute("SET lock_timeout = '500ms'")
                with self.assertRaises(Exception) as caught:
                    blocked.execute(
                        "UPDATE quant.raw_market_observations SET symbol = 'X' WHERE observation_id = 1"
                    )
                self.assertIn("lock", str(caught.exception).lower())
            finally:
                blocked.close()
        self.conn.execute("DROP TABLE IF EXISTS tier_batch")


if __name__ == "__main__":
    unittest.main()
