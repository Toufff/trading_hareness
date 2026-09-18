"""Pure-function coverage for scripts/database-storage-tiers.py (no database).

The tiering job deletes rows from the hot tables, so the decisions it makes --
where the cutoff falls, which tables a space shortage takes history from, and
how far the hot window may ever shrink -- are the part that must be provable
without a cluster.  The script is imported by path (the pattern
``test_migration_contracts.py`` uses for the migration modules); every psycopg
import inside it is lazy, so importing it here needs no database driver.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "database-storage-tiers.py"

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

    def test_twin_and_view_names_are_derived_not_configured(self):
        policy = tiers.TIER_POLICY[0]
        self.assertEqual(policy.cold_table, "quant.raw_market_observations_cold")
        self.assertEqual(policy.all_view, "quant.raw_market_observations_all")

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

    def test_above_the_high_water_mark_shortens_the_largest_table_to_the_target(self):
        verdict = tiers.plan_space_moves(90 * GB, 100 * GB, self.SIZES, self.DAYS)
        self.assertEqual(verdict["status"], "reduce")
        self.assertFalse(verdict["alert"])
        # 40 GB over a 400 day span is 0.1 GB a day; 15 GB back under the 75 %
        # target is 150 days, and only the larger table is touched.
        self.assertEqual(verdict["table_hot_days"], {"quant.a": 250})
        self.assertEqual(len(verdict["steps"]), 150)
        self.assertEqual({step["table"] for step in verdict["steps"]}, {"quant.a"})
        self.assertLessEqual(verdict["estimated_usage_bytes_after"], 75 * GB)
        self.assertEqual(verdict["steps"][-1]["hot_days"], 250)

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
        verdict = tiers.plan_space_moves(96 * GB, 100 * GB, self.SIZES, self.DAYS)
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

    def test_a_non_positive_budget_or_negative_usage_is_refused(self):
        with self.assertRaises(ValueError):
            tiers.plan_space_moves(1, 0, {}, {})
        with self.assertRaises(ValueError):
            tiers.plan_space_moves(-1, 100 * GB, {}, {})

    def test_thresholds_match_the_documented_policy(self):
        self.assertEqual(
            (tiers.HIGH_WATER_RATIO, tiers.TARGET_RATIO, tiers.ALERT_RATIO, tiers.MIN_HOT_DAYS),
            (0.85, 0.75, 0.95, 30),
        )


class ReadEnvFileTest(unittest.TestCase):
    def test_comments_quotes_and_blank_lines(self):
        import tempfile

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
