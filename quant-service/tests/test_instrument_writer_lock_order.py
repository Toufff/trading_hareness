"""The repository-wide guard on ``quant.instruments`` writers.

Three consecutive review rounds found a missed writer, and each time the
thing that hid it was the same: the set of writers lived in prose (a bullet
list in ``AGENTS.md``, a module docstring) or in a guard scoped to one file,
so a statement in a module nobody was looking at was invisible by
construction.  Round 2's finding *was* the drift; round 3 found
``trade_discipline.repository`` and two ``scripts/`` writers that appeared in
no list at all.

So this test does not enumerate writers and does not carry an allow-list.
It walks every ``.py`` under ``quant-service/app`` and ``scripts`` and holds
each occurrence of the instrument-insert statement to one mechanical rule:

    a writer either lives in ``quant-service/app/instrument_registry.py``
    -- the shared primitive, whose statements are the ones every other path
    is supposed to reuse -- or it sorts its own rows in the statement with
    ``ORDER BY 1`` before its ``ON CONFLICT`` clause.

``ORDER BY 1`` is the shared ascending lock order documented in
``instrument_registry``'s module docstring.  It is a correctness property:
``ON CONFLICT DO UPDATE`` row-locks every existing conflicting row and
``ON CONFLICT DO NOTHING`` locks the rows it genuinely inserts, so two
transactions touching an overlapping symbol set in different orders deadlock
-- three times in the owner PostgreSQL log on 2026-09-18.  Requiring the sort
in the SQL rather than in the Python that builds the array is what makes the
property checkable at all: a server-side sort of an already-sorted array is
free, and a writer that only sorts in Python cannot be distinguished from one
that forgot without reading its loop.

A NEW writer therefore fails here on the day it is written.  The two ways to
make it pass are the two ways that are correct: call
``instrument_registry.ensure_instruments`` / ``ensure_named_instruments``, or
write a set-based statement that carries ``ORDER BY 1``.  There is
deliberately no third way, and no mechanism for granting one.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

#: ``quant-service/tests/`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The two trees application and operator code lives in.  Test fixtures are
#: out of scope on purpose: a test that seeds an instrument row runs alone
#: against its own database and takes no lock any production path waits on.
SCANNED_ROOTS = (REPO_ROOT / "quant-service" / "app", REPO_ROOT / "scripts")

#: The one file whose statements ARE the shared primitive.  It is not an
#: exemption granted to a writer; it is where the rule is implemented (and
#: its own two statements carry ``ORDER BY 1`` anyway -- asserted below, so
#: this entry cannot quietly become a way to opt out).
REGISTRY = REPO_ROOT / "quant-service" / "app" / "instrument_registry.py"

NEEDLE = "INSERT INTO quant.instruments"

SKIP_PARTS = {"__pycache__", ".venv", "venv", "node_modules", ".git"}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if SKIP_PARTS.isdisjoint(path.parts):
                files.append(path)
    return files


def _occurrences() -> list[tuple[Path, int, str]]:
    """Every occurrence as ``(path, line number, the text that follows it)``.

    The trailing text is cut at the next occurrence so a statement missing
    its ``ON CONFLICT`` clause cannot borrow the next statement's.
    """
    found: list[tuple[Path, int, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        start = text.find(NEEDLE)
        while start != -1:
            following = text.find(NEEDLE, start + len(NEEDLE))
            tail = text[start + len(NEEDLE):following if following != -1 else len(text)]
            found.append((path, text.count("\n", 0, start) + 1, " ".join(tail.split())))
            start = following
    return found


class InstrumentWriterLockOrderTests(unittest.TestCase):
    def test_the_walk_reaches_both_trees(self) -> None:
        """A path bug must fail loudly, not pass vacuously.

        Without this the whole file would go green the moment ``rglob``
        returned nothing -- which is the failure mode of a guard that is
        supposed to notice absence.
        """
        for root in SCANNED_ROOTS:
            self.assertTrue(root.is_dir(), f"{root} is missing")
        self.assertTrue(REGISTRY.is_file(), f"{REGISTRY} is missing")
        scanned = _python_files()
        self.assertIn(REGISTRY, scanned)
        self.assertIn(REPO_ROOT / "scripts" / "import-adjusted-research-bars.py", scanned)
        self.assertIn(
            REPO_ROOT / "scripts" / "legacy" / "stock_brain" / "legacy_stock_brain_repository.py", scanned,
        )
        # Nested ``scripts`` subdirectories are reached, not just its top level.
        self.assertTrue(any(len(path.relative_to(REPO_ROOT).parts) > 3 for path in scanned))
        occurrences = _occurrences()
        self.assertTrue(any(path == REGISTRY for path, _line, _tail in occurrences))
        self.assertTrue(
            any(path.is_relative_to(REPO_ROOT / "scripts") for path, _line, _tail in occurrences),
            "the scripts tree had no instrument writer -- the walk or the needle is wrong",
        )

    def test_every_instrument_writer_takes_the_shared_lock_order(self) -> None:
        offenders: list[str] = []
        for path, line, tail in _occurrences():
            if path == REGISTRY:
                continue
            where = f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
            head, _sep, _rest = tail.partition("ON CONFLICT")
            if not _sep:
                offenders.append(f"{where}: no ON CONFLICT clause")
            elif "ORDER BY 1" not in head:
                offenders.append(f"{where}: no 'ORDER BY 1' before ON CONFLICT")
        self.assertEqual(offenders, [], "\n".join([
            "",
            "These quant.instruments writers do not take the shared ascending lock order.",
            "Fix one of two ways -- there is no allow-list to add them to:",
            "  * call app/instrument_registry.ensure_instruments (bare symbol registration)",
            "    or ensure_named_instruments (symbol + display name), or",
            "  * make the statement set-based (SELECT ... FROM unnest(...)/a stage table)",
            "    and put ORDER BY 1 between it and its ON CONFLICT clause.",
            *offenders,
        ]))

    def test_no_writer_sits_inside_a_loop(self) -> None:
        """``ORDER BY 1`` is necessary but not sufficient.

        A set-based statement executed once per item in a Python loop sends a
        one-row array each time: every row is trivially "sorted", and the
        transaction still takes its locks in the loop's order.  That is the
        exact shape every round of this work has had to undo, so it is checked
        structurally rather than by reading the text of the statement: no
        instrument-insert literal may be lexically nested inside a ``for`` or
        ``while`` body.

        This is a shape check, not a proof -- a writer can still be reached
        once per item through a helper or a caller loop, which is why the
        rule in ``AGENTS.md`` says the loop sorts when it owns the order (see
        ``scripts/trade-discipline.py``).  It catches the regression that
        actually keeps happening.
        """
        offenders: list[str] = []
        for path in _python_files():
            if path == REGISTRY:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Constant) and isinstance(inner.value, str) \
                            and NEEDLE in inner.value:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT).as_posix()}:{inner.lineno}: "
                            "instrument write inside a loop body"
                        )
        self.assertEqual(sorted(set(offenders)), [], "\n".join([
            "",
            "Hoist the registration out of the loop and hand the whole payload to",
            "app/instrument_registry.ensure_instruments / ensure_named_instruments,",
            "or to one set-based statement of your own.",
            *sorted(set(offenders)),
        ]))

    def test_the_registry_itself_is_not_an_exemption(self) -> None:
        """``instrument_registry`` is skipped above because its statements are
        the shared primitive -- so they must actually carry the order, or the
        skip would be a hole big enough to move any writer into."""
        # ``tail.startswith("(")`` is what separates the two SQL constants
        # from the module docstring's prose mention of the per-row form it
        # replaced: a real statement continues into its column list.
        statements = [
            tail for path, _line, tail in _occurrences()
            if path == REGISTRY and tail.startswith("(")
        ]
        self.assertEqual(len(statements), 2, "instrument_registry's SQL constants changed shape")
        for tail in statements:
            head, _sep, _rest = tail.partition("ON CONFLICT")
            self.assertIn("ORDER BY 1", head)
        actions = [tail.partition("ON CONFLICT")[2][:40] for tail in statements]
        self.assertEqual(sum("DO NOTHING" in action for action in actions), 1)
        self.assertEqual(sum("DO UPDATE" in action for action in actions), 1)

    def test_agents_md_states_the_rule_rather_than_a_roster(self) -> None:
        """The document that used to carry the roster must point at this test.

        Three rounds of drift came from ``AGENTS.md`` reading as an
        exhaustive partition of the writers.  If someone reinstates a
        "not converted" list there, the roster is back and so is the drift.
        """
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("test_instrument_writer_lock_order", agents)
        self.assertNotIn("Not converted", agents)


if __name__ == "__main__":  # pragma: no cover - direct execution convenience
    unittest.main()
