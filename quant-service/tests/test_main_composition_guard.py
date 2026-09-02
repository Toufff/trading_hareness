"""Structural guardrails against main.py drifting back into a monolith.

WP9b removed every ``*_legacy`` alias (dead compatibility scaffolding) and a
first slice of ``while True`` polling loops / direct-SQL business logic into
dedicated ``*_runtime.py``/``*_service.py`` modules. WP9c continued that
extraction (xiaojie leader-flow, intraday signal attribution, the daily
control-plane sync, watchlist history hydration and the rule-input replay
runner all moved out of the composition root), but did not finish extracting
every remaining ``connection.execute(`` call or background-loop body (see
wp9c-report.md "未处理"). Rather than assert the not-yet-reached end state
(zero of either), this guard freezes the *current* counts as a ceiling: it
fails on regression (more direct SQL, more inline loops, a ``_legacy`` alias
coming back, or unbounded file growth) without blocking on work this pass did
not have time to finish.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

MAIN_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"

#: Ceilings captured immediately after WP9c's cleanup pass. Lower these as
#: further business logic and direct SQL move out of main.py; never raise
#: them to accommodate new code moving in.
MAX_CONNECTION_EXECUTE_CALLS = 15
MAX_WHILE_TRUE_LOOPS = 1
#: Actual line count right after this pass, plus a small buffer so routine
#: single-line edits do not immediately trip the guard.
MAX_LINE_COUNT = 5124


class MainCompositionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = MAIN_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_legacy_alias_functions(self) -> None:
        """The 16 unused ``*_legacy`` forwarders WP9b deleted must not return."""
        legacy_names = {
            node.name
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.endswith("_legacy")
        }
        self.assertEqual(legacy_names, set())

    def test_connection_execute_calls_do_not_regress(self) -> None:
        """Direct SQL in the composition root should shrink, never grow."""
        calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "connection"
        ]
        self.assertLessEqual(
            len(calls), MAX_CONNECTION_EXECUTE_CALLS,
            f"main.py grew new connection.execute(...) call sites (found {len(calls)}, "
            f"ceiling {MAX_CONNECTION_EXECUTE_CALLS}); move new business SQL into a dedicated module instead.",
        )

    def test_while_true_loops_do_not_regress(self) -> None:
        """Inline polling loops should move to *_runtime.py, never grow in place."""
        loops = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value is True
        ]
        self.assertLessEqual(
            len(loops), MAX_WHILE_TRUE_LOOPS,
            f"main.py grew new inline `while True` loops (found {len(loops)}, ceiling {MAX_WHILE_TRUE_LOOPS}); "
            "compose an injected runtime module's loop instead.",
        )

    def test_file_length_does_not_regress(self) -> None:
        """A composition root should shrink over time, not silently regrow."""
        line_count = self.source.count("\n") + 1
        self.assertLessEqual(
            line_count, MAX_LINE_COUNT,
            f"app/main.py grew to {line_count} lines (ceiling {MAX_LINE_COUNT}); "
            "move new business logic into a dedicated module instead of main.py.",
        )


if __name__ == "__main__":
    unittest.main()
