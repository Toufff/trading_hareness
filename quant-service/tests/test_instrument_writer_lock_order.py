"""The repository-wide guard on ``quant.instruments`` writers.

Three consecutive review rounds found a missed writer, and each time the
thing that hid it was the same: the set of writers lived in prose (a bullet
list in ``AGENTS.md``, a module docstring) or in a guard scoped to one file,
so a statement in a module nobody was looking at was invisible by
construction.  Round 2's finding *was* the drift; round 3 found
``trade_discipline.repository`` and two ``scripts/`` writers that appeared in
no list at all.

So this test does not enumerate writers and does not carry an allow-list.
It walks every ``.py`` in the repository -- ``quant-service`` (``app/`` and
the top-level ``database_bootstrap``/``entrypoint``/``retention_maintenance``
modules that open a connection to the same database), ``scripts``,
``legacy``, ``workflows``, ``deploy`` -- and holds each occurrence of the
instrument-insert statement to one mechanical rule:

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
import os
import re
from pathlib import Path
import unittest

#: ``quant-service/tests/`` -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The whole repository.  Scoping the walk to ``app`` and ``scripts`` was the
#: last thing narrowing it: the rule is about every session that opens a
#: connection to this database, and ``quant-service/database_bootstrap.py``
#: and ``entrypoint.py`` do that from outside ``app/``, as could anything
#: dropped into ``legacy/`` or ``workflows/``.  Directories are pruned below
#: instead of the roots being enumerated here.
SCANNED_ROOTS = (REPO_ROOT,)

#: The one file whose statements ARE the shared primitive.  It is not an
#: exemption granted to a writer; it is where the rule is implemented (and
#: its own two statements carry ``ORDER BY 1`` anyway -- asserted below, so
#: this entry cannot quietly become a way to opt out).
REGISTRY = REPO_ROOT / "quant-service" / "app" / "instrument_registry.py"

#: Case-insensitive and whitespace-tolerant on purpose.  A contiguous
#: ``str.find("INSERT INTO quant.instruments")`` matched exactly one
#: spelling, so a lowercase statement -- or ``INSERT  INTO quant . instruments``
#: -- walked straight past the guard.  SQL keywords and whitespace are not
#: significant to the server, so they must not be significant here either.
NEEDLE = re.compile(r"insert\s+into\s+quant\s*\.\s*instruments", re.IGNORECASE)

#: The fingerprint an interpolated statement always leaves behind, whatever
#: it builds the schema name out of.  A writer composed at runtime
#: (``f"INSERT INTO {SCHEMA}.instruments(...)"``) is invisible to ``NEEDLE``
#: by construction, because the table name is not in the source at all.  The
#: schema here is fixed, so there is no legitimate reason to build one: it is
#: rejected outright rather than checked for ``ORDER BY 1``.
INTERPOLATED_TABLE = ".instruments("

#: Pruned whole, never descended into: build output, vendored trees, and
#: tests.  Test fixtures are out of scope on purpose -- a test that seeds an
#: instrument row runs alone against its own database and takes no lock any
#: production path waits on.
SKIP_PARTS = {
    "__pycache__", ".venv", "venv", "node_modules", ".git", "artifacts", "frontend",
    "dist", "build", "tests",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for directory, subdirectories, names in os.walk(root):
            subdirectories[:] = sorted(name for name in subdirectories if name not in SKIP_PARTS)
            files.extend(Path(directory) / name for name in sorted(names) if name.endswith(".py"))
    return files


def _occurrences() -> list[tuple[Path, int, str]]:
    """Every occurrence as ``(path, line number, the text that follows it)``.

    The trailing text is cut at the next occurrence so a statement missing
    its ``ON CONFLICT`` clause cannot borrow the next statement's.
    """
    found: list[tuple[Path, int, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        matches = list(NEEDLE.finditer(text))
        for index, match in enumerate(matches):
            stop = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            tail = text[match.end():stop]
            found.append((path, text.count("\n", 0, match.start()) + 1, " ".join(tail.split())))
    return found


def _interpolated_statement_parts() -> list[tuple[Path, int, str]]:
    """String literals that build a ``*.instruments(...)`` statement at runtime.

    ``f"INSERT INTO {SCHEMA}.instruments(...)"`` and
    ``"INSERT INTO " + SCHEMA + ".instruments(...)"`` both write this table
    and neither can be found by a search over the source, because the table
    name is never in the source.  Both leave the same fragment behind.
    """
    found: list[tuple[Path, int, str]] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                pieces = [value for value in node.values
                          if isinstance(value, ast.Constant) and isinstance(value.value, str)]
            elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                pieces = [value for value in ast.walk(node)
                          if isinstance(value, ast.Constant) and isinstance(value.value, str)]
            else:
                continue
            for piece in pieces:
                index = piece.value.find(INTERPOLATED_TABLE)
                # A fragment whose ``.instruments(`` is already preceded by
                # ``quant`` inside the SAME literal spelled its table name
                # out -- it is an ordinary constant that happens to sit in an
                # interpolated statement (a migration's ``REFERENCES
                # quant.instruments(symbol)``), and ``NEEDLE`` can see it.
                # What is rejected here is the schema arriving from outside
                # the literal, which is what makes a writer unsearchable.
                if index != -1 and not piece.value[:index].rstrip().lower().endswith("quant"):
                    found.append((path, piece.lineno, piece.value))
    return found


class InstrumentWriterLockOrderTests(unittest.TestCase):
    def test_the_walk_reaches_every_tree_that_can_hold_a_writer(self) -> None:
        """A path bug must fail loudly, not pass vacuously.

        Without this the whole file would go green the moment the walk
        returned nothing -- which is the failure mode of a guard that is
        supposed to notice absence.  Each name below is a place a writer has
        actually been found or could plausibly be put; a future narrowing of
        the walk fails here rather than passing quietly.
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
        # Outside ``app/`` but on the same database: the two modules that
        # migrate and start the service.
        self.assertIn(REPO_ROOT / "quant-service" / "database_bootstrap.py", scanned)
        self.assertIn(REPO_ROOT / "quant-service" / "entrypoint.py", scanned)
        # The top-level trees that carry no writer today and are exactly where
        # "put the new helper next to the old one" would land one.
        self.assertIn(REPO_ROOT / "legacy" / "macos" / "scripts" / "svc_supervisor.py", scanned)
        # Nested ``scripts`` subdirectories are reached, not just its top level.
        self.assertTrue(any(len(path.relative_to(REPO_ROOT).parts) > 3 for path in scanned))
        # ...and the pruned directories really are pruned, so the walk stays
        # a guard and does not turn into a scan of vendored trees.
        self.assertFalse([path for path in scanned if SKIP_PARTS & set(path.parts)])
        occurrences = _occurrences()
        self.assertTrue(any(path == REGISTRY for path, _line, _tail in occurrences))
        self.assertTrue(
            any(path.is_relative_to(REPO_ROOT / "scripts") for path, _line, _tail in occurrences),
            "the scripts tree had no instrument writer -- the walk or the needle is wrong",
        )

    def test_the_needle_ignores_case_and_whitespace(self) -> None:
        """The two spellings that used to walk straight past the guard.

        A contiguous case-sensitive ``str.find`` is not a search for this
        statement, it is a search for one way of typing it -- and both of
        these reach exactly the same table.
        """
        for spelling in (
            "insert into quant.instruments(symbol,exchange,source) values(%s,%s,%s)",
            "INSERT  INTO   quant . instruments (symbol) VALUES(%s)",
            "Insert Into quant.Instruments(symbol)",
        ):
            self.assertIsNotNone(NEEDLE.search(spelling), spelling)
        for innocent in (
            "SELECT symbol FROM quant.instruments",
            "INSERT INTO quant.instrument_lifecycle_evidence(symbol)",
            "INSERT INTO other.instruments(symbol)",
        ):
            self.assertIsNone(NEEDLE.search(innocent), innocent)

    def test_every_instrument_writer_takes_the_shared_lock_order(self) -> None:
        offenders: list[str] = []
        for path, line, tail in _occurrences():
            if path == REGISTRY:
                continue
            where = f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
            # Upper-cased for the same reason ``NEEDLE`` ignores case: SQL
            # keywords do, so a lowercase writer must be reported for the
            # reason it is actually wrong ("no ORDER BY 1") rather than
            # mis-reported as having no conflict clause at all.
            head, _sep, _rest = tail.upper().partition("ON CONFLICT")
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
        rule in ``AGENTS.md`` says the loop sorts when it owns the order.
        The two caller loops that exist are pinned by their own tests
        (``tests/test_daily_bar_caller_lock_order.py`` and
        ``tests/test_trade_discipline_cli.py``).  This one catches the
        regression that actually keeps happening.
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
                            and NEEDLE.search(inner.value):
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

    def test_no_writer_builds_its_table_name_at_runtime(self) -> None:
        """An interpolated statement is rejected outright, not checked.

        ``f"INSERT INTO {SCHEMA}.instruments(...)"`` writes this table and is
        invisible to every text search over the source, because the table
        name is not in the source.  The schema is a constant here -- there is
        no supported multi-schema deployment -- so composing the name is
        always either a mistake or a way around this guard, and either way
        the answer is the same: write the table name.
        """
        offenders = [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {fragment.strip()[:80]}"
            for path, line, fragment in _interpolated_statement_parts()
        ]
        self.assertEqual(offenders, [], "\n".join([
            "",
            "A quant.instruments writer must spell its table name literally, so that",
            "this guard can see it at all.  Call app/instrument_registry instead, or",
            "write 'INSERT INTO quant.instruments' out with ORDER BY 1.",
            *offenders,
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
            head, _sep, _rest = tail.upper().partition("ON CONFLICT")
            self.assertIn("ORDER BY 1", head)
        actions = [tail.upper().partition("ON CONFLICT")[2][:40] for tail in statements]
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
