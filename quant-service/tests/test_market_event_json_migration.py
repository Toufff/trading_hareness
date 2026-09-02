from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import alembic.op


MIGRATION = (
    Path(__file__).parents[1]
    / "migrations" / "versions" / "20260816_0036_sanitize_market_event_json.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0036", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _captured_statements(function) -> list[str]:
    statements: list[str] = []
    with patch.object(alembic.op, "execute", lambda sql, *args, **kwargs: statements.append(sql)):
        function()
    return statements


def _regexp_replace_calls(sql: str) -> list[tuple[str, str]]:
    """Return (pattern, replacement) pairs in evaluation order (innermost first)."""
    pairs = re.findall(r"'((?:[^']|'')+)',\s*E'((?:[^']|'')*)',\s*'g'", sql)
    return [(pattern, replacement) for pattern, replacement in reversed(pairs)]


def _apply_in_python(sql: str, body: str) -> str:
    """Execute the migration's regexp_replace chain with Python's regex engine."""
    for pattern, replacement in _regexp_replace_calls(sql):
        python_pattern = pattern.replace("[[:space:]]", r"\s")
        python_replacement = replacement.replace("\\\\1", r"\1").replace("\\\\2", r"\2")
        body = re.sub(python_pattern, python_replacement, body)
    return body


class MarketEventJsonMigrationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_migration()
        self.statements = _captured_statements(self.module.upgrade)

    def test_upgrade_is_one_guarded_update_of_market_events(self):
        self.assertEqual(len(self.statements), 1)
        sql = " ".join(self.statements[0].split())
        self.assertRegex(sql, r"^UPDATE quant\.market_events SET body = regexp_replace\(")
        # Only rows that are not valid JSON and actually carry a non-finite token are touched.
        where = sql.split(" WHERE ", 1)[1]
        self.assertIn("body IS NOT JSON", where)
        self.assertIn("body ~ ':[[:space:]]*(-?Infinity|NaN)[[:space:]]*[,}]'", where)
        self.assertEqual(len(_regexp_replace_calls(self.statements[0])), 3)

    def test_replacement_chain_normalizes_every_non_finite_literal(self):
        sql = self.statements[0]
        body = '{"a": NaN, "b": Infinity, "c":-Infinity,"d": 1.5, "e": "NaN", "f": {"g": NaN}}'
        self.assertEqual(
            _apply_in_python(sql, body),
            '{"a": null, "b": null, "c":null,"d": 1.5, "e": "NaN", "f": {"g": null}}',
        )

    def test_replacement_chain_leaves_finite_json_untouched(self):
        sql = self.statements[0]
        body = '{"price": 12.5, "name": "Infinity Ltd", "ratio": -1e9, "flag": null}'
        self.assertEqual(_apply_in_python(sql, body), body)

    def test_downgrade_is_an_explicit_no_op(self):
        self.assertEqual(_captured_statements(self.module.downgrade), [])
        self.assertEqual(self.module.down_revision, "20260816_0035")
        self.assertEqual(self.module.revision, "20260816_0036")


if __name__ == "__main__":
    unittest.main()
