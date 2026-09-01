import unittest

from database_bootstrap import (
    BASELINE_PREREQUISITES_SQL,
    REQUIRED_BASELINE_TABLES,
    bootstrap_action,
)


class DatabaseBootstrapTests(unittest.TestCase):
    def test_versioned_schema_only_runs_upgrades(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=True,
                quant_table_count=200,
                required_table_count=len(REQUIRED_BASELINE_TABLES),
            ),
            "upgrade",
        )

    def test_empty_schema_creates_frozen_baseline(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=False,
                quant_table_count=0,
                required_table_count=0,
            ),
            "create_baseline",
        )

    def test_complete_unversioned_baseline_is_resumable(self):
        self.assertEqual(
            bootstrap_action(
                version_table_present=False,
                quant_table_count=len(REQUIRED_BASELINE_TABLES),
                required_table_count=len(REQUIRED_BASELINE_TABLES),
            ),
            "stamp_existing",
        )

    def test_partial_unversioned_schema_is_never_stamped(self):
        with self.assertRaisesRegex(RuntimeError, "partial"):
            bootstrap_action(
                version_table_present=False,
                quant_table_count=3,
                required_table_count=2,
            )

    def test_fresh_baseline_prerequisites_define_sector_parents(self):
        self.assertLess(
            BASELINE_PREREQUISITES_SQL.index("quant.sector_taxonomies"),
            BASELINE_PREREQUISITES_SQL.index("quant.sectors"),
        )


if __name__ == "__main__":
    unittest.main()
