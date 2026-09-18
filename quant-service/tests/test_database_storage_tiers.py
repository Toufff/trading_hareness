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

The end-to-end behaviour (conservation, interruption, quarantine, deadline,
drift) is exercised against a scratch database; see the branch report.
"""

from __future__ import annotations

import importlib.util
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
    def _record(tables=(), errors=(), space="ok", quarantined=0, deadline=False):
        return {
            "tables": [{"status": status} for status in tables],
            "errors": list(errors),
            "space_policy": {"status": space},
            "quarantined_rows": quarantined,
            "deadline_reached": deadline,
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

    ``psycopg.sql`` objects are rendered without a connection here; that is
    enough to pin the shape the safety argument depends on.  The behaviour is
    proven against a scratch database separately.
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


if __name__ == "__main__":
    unittest.main()
