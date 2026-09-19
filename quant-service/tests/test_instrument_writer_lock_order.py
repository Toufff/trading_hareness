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

**Everything here reads the parsed source, not the file's raw text.**  A
statement lives in a string literal, so that is where it is looked for: a
match can then never run past the end of the literal it was found in and
borrow an unrelated later statement's ``ON CONFLICT`` clause (the round-4
finding), and adjacent-literal concatenation -- ``"INSERT INTO quant."
"instruments(...)"``, which no text search can see -- is already one
``ast.Constant`` by the time the parser is done.  The one thing the raw text
had over this is that it did not need the file to parse, which is handled
instead by reporting an unparseable file as a failure of that file.
"""

from __future__ import annotations

import ast
import functools
import os
import re
import tempfile
from pathlib import Path
from typing import Iterable
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

#: Case-insensitive, whitespace-tolerant and quoting-tolerant on purpose.  A
#: contiguous ``str.find("INSERT INTO quant.instruments")`` matched exactly
#: one spelling, so a lowercase statement -- or ``INSERT  INTO quant .
#: instruments``, or the equally valid ``quant."instruments"`` /
#: ``"quant"."instruments"`` -- walked straight past the guard.  SQL keywords,
#: whitespace and identifier quoting are not significant to the server, so
#: they must not be significant here either.  The ``\b`` sits BEFORE the
#: closing quote so it still anchors when the quote is there: it is what
#: keeps ``quant.instrument_lifecycle_evidence`` and any future
#: ``quant.instruments_cold`` sibling out.
NEEDLE = re.compile(r"insert\s+into\s+\"?quant\"?\s*\.\s*\"?instruments\b\"?", re.IGNORECASE)

#: What an interpolated field becomes when a string expression is rendered as
#: the statement it will be at runtime: an f-string's ``{...}``, a ``+``
#: operand that is not a literal, a ``%s`` in a ``%``-formatted template, a
#: ``{}`` field in a ``.format()`` template.  A private-use code point, so it
#: cannot collide with anything a real statement contains.
PLACEHOLDER = ""

#: The fingerprint an instrument writer leaves in its rendered statement.  A
#: writer composed at runtime (``f"INSERT INTO {SCHEMA}.instruments(...)"``,
#: ``"INSERT INTO %s.instruments(...)" % schema``,
#: ``"INSERT INTO {}.instruments(...)".format(schema)``) is invisible to
#: ``NEEDLE`` by construction, because the table name is not in the source at
#: all.  The schema here is fixed, so there is no legitimate reason to build
#: one: a match whose schema part carries ``PLACEHOLDER`` is rejected
#: outright rather than checked for ``ORDER BY 1``.  The column list is NOT
#: part of the fingerprint -- ``f"INSERT INTO {S}.instruments SELECT ..."``
#: has none and is the same evasion.
RUNTIME_TABLE = re.compile(
    r"insert\s+into\s+(?P<schema>[^\s;()]*)\s*\.\s*\"?instruments\b\"?",
    re.IGNORECASE,
)

#: ``%``-style and ``str.format`` fields, replaced by ``PLACEHOLDER`` before a
#: template is matched.  Applied ONLY to a literal that is actually the left
#: operand of ``%`` or the receiver of ``.format()``: every psycopg statement
#: in this repository is full of ``%s`` parameter markers, and those are not
#: interpolation -- the value never reaches the SQL text.
PERCENT_FIELD = re.compile(r"%(?:\([^)]*\))?[-+ #0]*[0-9*]*(?:\.[0-9*]+)?[hlL]?[a-zA-Z%]")
FORMAT_FIELD = re.compile(r"\{[^{}]*\}")

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


def _parse(paths: Iterable[Path]) -> tuple[list[tuple[Path, ast.Module]], list[str]]:
    """Parse every file, and report the ones that cannot be read or parsed.

    The walk covers ~760 files across five trees, so it will sooner or later
    meet a file that is not valid UTF-8 or not valid Python for this
    interpreter.  Letting that escape turns a lock-order guard into an
    unrelated ``SyntaxError`` from whichever test happened to reach the file
    first, which says nothing about lock order and does not even say which
    file it was.  Each failure is named here instead and asserted on by
    ``test_the_walk_reaches_every_tree_that_can_hold_a_writer``, so the guard
    still fails loudly -- for the right reason, about the right file.
    """
    trees: list[tuple[Path, ast.Module]] = []
    failures: list[str] = []
    for path in paths:
        try:
            name = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:  # pragma: no cover - only synthetic probes
            name = path.as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            failures.append(f"{name}: unreadable as utf-8 ({type(error).__name__}: {error})")
            continue
        try:
            trees.append((path, ast.parse(source, filename=str(path))))
        except SyntaxError as error:
            failures.append(f"{name}:{error.lineno}: does not parse ({error.msg})")
    return trees, failures


@functools.lru_cache(maxsize=1)
def _parsed() -> tuple[tuple[tuple[Path, ast.Module], ...], tuple[str, ...]]:
    """The whole walk, parsed once for every check in this file."""
    trees, failures = _parse(_python_files())
    return tuple(trees), tuple(failures)


def _string_constants(tree: ast.AST) -> list[ast.Constant]:
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _occurrences_in(path: Path, tree: ast.AST) -> list[tuple[Path, int, str]]:
    """Every occurrence as ``(path, line number, the text that follows it)``.

    The trailing text ends where the statement's own string literal ends, or
    at the next occurrence inside that same literal -- never later.  Bounding
    it at the next occurrence *in the file* (what this did before) let the
    last writer in a file read to EOF, so an unrelated statement further down
    could hand it an ``ON CONFLICT`` clause it does not have.
    """
    found: list[tuple[Path, int, str]] = []
    for node in _string_constants(tree):
        value = node.value
        matches = list(NEEDLE.finditer(value))
        for index, match in enumerate(matches):
            stop = matches[index + 1].start() if index + 1 < len(matches) else len(value)
            # Newlines inside the literal are real source lines; implicit
            # concatenation of several literals is not, so this is the first
            # line of the literal plus what the value itself accounts for.
            line = node.lineno + value.count("\n", 0, match.start())
            found.append((path, line, " ".join(value[match.end():stop].split())))
    return found


def _render(node: ast.AST) -> str:
    """The text a string expression will have at runtime, interpolations blanked."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else PLACEHOLDER
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(value) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        return PLACEHOLDER
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render(node.left) + _render(node.right)
    return PLACEHOLDER


def _is_template(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)) \
        or isinstance(node, ast.JoinedStr)


def _runtime_table_statements_in(path: Path, tree: ast.AST) -> list[tuple[Path, int, str]]:
    """String expressions that build a ``<something>.instruments`` write at runtime.

    Four spellings reach this table without ever naming it in the source:
    an f-string, ``+`` concatenation, ``%`` formatting and ``str.format``.
    All four are rendered to the statement they produce and matched there, so
    the check is about what runs rather than about how it was typed.
    """
    found: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = _render(node)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            text = _render(node)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and _is_template(node.left):
            text = PERCENT_FIELD.sub(PLACEHOLDER, _render(node.left))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "format" and _is_template(node.func.value)):
            text = FORMAT_FIELD.sub(PLACEHOLDER, _render(node.func.value))
        else:
            continue
        for match in RUNTIME_TABLE.finditer(text):
            # A schema spelled out in the source -- ``quant``, or a migration's
            # ``REFERENCES quant.instruments(symbol)`` sitting inside an
            # otherwise interpolated statement -- is an ordinary literal that
            # ``NEEDLE`` can see.  What is rejected here is the schema
            # arriving from outside the text, which is what makes a writer
            # unsearchable.
            if PLACEHOLDER in match.group("schema"):
                found[node.lineno] = " ".join(text.split())
    return [(path, line, text) for line, text in sorted(found.items())]


def _occurrences() -> list[tuple[Path, int, str]]:
    trees, _failures = _parsed()
    return sorted(
        occurrence for path, tree in trees for occurrence in _occurrences_in(path, tree)
    )


def _interpolated_statement_parts() -> list[tuple[Path, int, str]]:
    trees, _failures = _parsed()
    return sorted(
        part for path, tree in trees for part in _runtime_table_statements_in(path, tree)
    )


#: The one function every writer statement is executed through.  Sorting is
#: only a property of writers that sort; the peer's old per-row registration
#: does not, and against it the owner needs a lock wait that stays below
#: ``deadlock_timeout`` and a savepoint to roll back to (see
#: ``app/instrument_lock_retry.py``).  So the guard also requires that no
#: statement found by ``NEEDLE`` is ever handed to a bare ``execute``.
LOCK_RETRY_HELPER = "execute_instrument_write"

#: What "executed directly" means: psycopg's execution entry points.
DIRECT_EXECUTION = frozenset({"execute", "executemany", "copy", "stream"})


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _helper_query(call: ast.AST | None) -> ast.AST | None:
    """The ``query`` argument of an ``execute_instrument_write(target, query, ...)`` call."""
    if not isinstance(call, ast.Call) or _call_name(call) != LOCK_RETRY_HELPER:
        return None
    if len(call.args) >= 2:
        return call.args[1]
    return next((keyword.value for keyword in call.keywords if keyword.arg == "query"), None)


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_docstring(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    """Prose about the statement, never executed."""
    statement = parents.get(node)
    owner = parents.get(statement) if statement is not None else None
    return (
        isinstance(statement, ast.Expr)
        and isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and bool(owner.body) and owner.body[0] is statement
    )


def _referenced_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _writer_constants_in(tree: ast.Module) -> dict[str, int]:
    """Module-level ``NAME = "<instrument writer SQL>"`` bindings -> line."""
    found: dict[str, int] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) \
                and isinstance(value.value, str) and NEEDLE.search(value.value):
            found[target.id] = statement.lineno
    return found


def _display(path: Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _unrouted_writers_in(files: Iterable[tuple[Path, ast.Module]]) -> list[str]:
    """Every instrument-writer statement not executed through the retry helper.

    Three shapes are accepted and nothing else:

    * the literal IS the ``query`` argument of ``execute_instrument_write``;
    * the literal is bound to a module-level constant, that constant is the
      ``query`` argument of ``execute_instrument_write`` somewhere in its own
      module, and nowhere in the walk -- that module or one importing it --
      is it handed to a bare ``execute``/``executemany``/``copy``/``stream``;
    * the literal is a docstring (prose, never executed).

    A literal held in a local variable, a list or a dict, or passed straight
    to ``execute``, is reported: none of those can be seen to be routed.
    """
    files = list(files)
    offenders: list[str] = []
    constants: dict[str, str] = {}
    for path, tree in files:
        where = _display(path)
        parents = _parent_map(tree)
        module_constants = _writer_constants_in(tree)
        for name, line in module_constants.items():
            constants[name] = f"{where}:{line}"
        for node in _string_constants(tree):
            if not NEEDLE.search(node.value):
                continue
            parent = parents.get(node)
            call = parents.get(parent) if isinstance(parent, ast.keyword) else parent
            if _helper_query(call) is node or _is_docstring(node, parents):
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent in tree.body:
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id in module_constants:
                    continue
            offenders.append(f"{where}:{node.lineno}: executed outside {LOCK_RETRY_HELPER}")
        routed_names = {
            _referenced_name(_helper_query(call))
            for call in ast.walk(tree) if isinstance(call, ast.Call)
        }
        for name, line in module_constants.items():
            if name not in routed_names:
                offenders.append(f"{where}:{line}: {name} is never passed to {LOCK_RETRY_HELPER}")
    for path, tree in files:
        where = _display(path)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or _call_name(call) not in DIRECT_EXECUTION:
                continue
            for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
                name = _referenced_name(argument)
                if name in constants:
                    offenders.append(
                        f"{where}:{call.lineno}: {name} ({constants[name]}) passed to "
                        f".{_call_name(call)}() instead of {LOCK_RETRY_HELPER}"
                    )
    return sorted(set(offenders))


def _probe(source: str, name: str = "probe.py") -> tuple[Path, ast.Module]:
    """Parse a synthetic module, for the negative controls below."""
    return Path(name), ast.parse(source, filename=name)


#: A well-formed writer statement for the routing negative controls.
STATEMENT_PROBE = "INSERT INTO quant.instruments(symbol) SELECT * FROM unnest(%s::text[]) ORDER BY 1 ON CONFLICT(symbol) DO NOTHING"


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
        trees, failures = _parsed()
        # Every file that is in the walk must be READABLE by the walk, or the
        # rest of this file is checking a smaller repository than it thinks.
        self.assertEqual(list(failures), [], "\n".join([
            "",
            "These files are inside the lock-order walk but could not be read as",
            "utf-8 Python, so no check in this file looked at them.  Fix the file,",
            "or add its directory to SKIP_PARTS if it is not a tree that can hold a",
            "quant.instruments writer.",
            *failures,
        ]))
        self.assertEqual(len(trees), len(scanned))
        occurrences = _occurrences()
        self.assertTrue(any(path == REGISTRY for path, _line, _tail in occurrences))
        self.assertTrue(
            any(path.is_relative_to(REPO_ROOT / "scripts") for path, _line, _tail in occurrences),
            "the scripts tree had no instrument writer -- the walk or the needle is wrong",
        )

    def test_an_unreadable_file_is_named_rather_than_raising_somewhere_else(self) -> None:
        """The report says which file, and the other files still get parsed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.py"
            good.write_text("SQL = 'INSERT INTO quant.instruments(symbol)'\n", encoding="utf-8")
            broken = root / "broken.py"
            broken.write_text("def f(:\n", encoding="utf-8")
            undecodable = root / "undecodable.py"
            undecodable.write_bytes(b"x = '\xff\xfe not utf-8'\n")
            trees, failures = _parse([good, broken, undecodable])
        self.assertEqual([path for path, _tree in trees], [good])
        self.assertEqual(len(failures), 2, failures)
        self.assertTrue(any("broken.py" in failure and "does not parse" in failure
                            for failure in failures), failures)
        self.assertTrue(any("undecodable.py" in failure and "utf-8" in failure
                            for failure in failures), failures)

    def test_the_needle_ignores_case_whitespace_and_quoting(self) -> None:
        """The spellings that used to walk straight past the guard.

        A contiguous case-sensitive ``str.find`` is not a search for this
        statement, it is a search for one way of typing it -- and every one
        of these reaches exactly the same table.
        """
        for spelling in (
            "insert into quant.instruments(symbol,exchange,source) values(%s,%s,%s)",
            "INSERT  INTO   quant . instruments (symbol) VALUES(%s)",
            "Insert Into quant.Instruments(symbol)",
            'INSERT INTO quant."instruments"(symbol) VALUES(%s)',
            'INSERT INTO "quant"."instruments"(symbol) VALUES(%s)',
            'INSERT INTO "quant" . "instruments" SELECT * FROM staging',
        ):
            self.assertIsNotNone(NEEDLE.search(spelling), spelling)
        for innocent in (
            "SELECT symbol FROM quant.instruments",
            "INSERT INTO quant.instrument_lifecycle_evidence(symbol)",
            "INSERT INTO other.instruments(symbol)",
            # The storage-tier twin naming convention, which is a different
            # table and must not be reported as this one.
            "INSERT INTO quant.instruments_cold(symbol)",
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

    def test_a_writer_cannot_borrow_a_later_statements_conflict_clause(self) -> None:
        """The round-4 hole: a tail that ran to the end of the FILE.

        The last needle in a file had no next needle to stop at, so its tail
        was everything after it -- and any later, unrelated statement
        carrying ``ORDER BY 1 ... ON CONFLICT`` silently satisfied the check
        for a writer that has no conflict clause at all.  Each match now ends
        with its own literal.
        """
        path, tree = _probe(
            'BARE = "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,%s)"\n'
            'OTHER = ("INSERT INTO quant.other_table(symbol) '
            'SELECT * FROM unnest(%s::text[]) ORDER BY 1 ON CONFLICT(symbol) DO NOTHING")\n'
        )
        occurrences = _occurrences_in(path, tree)
        self.assertEqual(len(occurrences), 1, occurrences)
        _path, line, tail = occurrences[0]
        self.assertEqual(line, 1)
        self.assertNotIn("ON CONFLICT", tail.upper())
        # ...and two writers inside ONE literal still bound each other, so a
        # first statement cannot read into the second either.
        path, tree = _probe(
            'SQL = """\n'
            'INSERT INTO quant.instruments(symbol) VALUES(%s);\n'
            'INSERT INTO quant.instruments(symbol) SELECT * FROM unnest(%s::text[])\n'
            '  ORDER BY 1 ON CONFLICT(symbol) DO NOTHING;\n'
            '"""\n'
        )
        first, second = _occurrences_in(path, tree)
        self.assertEqual((first[1], second[1]), (2, 3))
        self.assertNotIn("ON CONFLICT", first[2].upper())
        self.assertIn("ORDER BY 1", second[2].upper())

    def test_adjacent_literal_concatenation_is_one_statement(self) -> None:
        """What reading the parsed source buys: the spelling no text search sees.

        ``"INSERT INTO quant." "instruments(...)"`` is a single string by the
        time the parser is done, so it is matched -- and its ``ON CONFLICT``
        clause, written in yet another adjacent literal, is found in the same
        value rather than in "the text after the match".
        """
        path, tree = _probe(
            'SQL = (\n'
            '    "INSERT INTO quant."\n'
            '    "instruments(symbol,exchange) "\n'
            '    "SELECT * FROM unnest(%s::text[],%s::text[]) ORDER BY 1 "\n'
            '    "ON CONFLICT(symbol) DO NOTHING"\n'
            ')\n'
        )
        occurrences = _occurrences_in(path, tree)
        self.assertEqual(len(occurrences), 1, occurrences)
        head, sep, _rest = occurrences[0][2].upper().partition("ON CONFLICT")
        self.assertTrue(sep)
        self.assertIn("ORDER BY 1", head)

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
        trees, _failures = _parsed()
        for path, tree in trees:
            if path == REGISTRY:
                continue
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

    def test_every_interpolation_spelling_is_rejected(self) -> None:
        """Four ways to compose the table name, one answer to all four.

        The check used to see only an f-string and a ``+`` concatenation, and
        only when the fragment carried a column list, so
        ``"INSERT INTO %s.instruments(...)" % schema``,
        ``"INSERT INTO {}.instruments(...)".format(schema)`` and a
        column-less ``f"INSERT INTO {S}.instruments SELECT ..."`` all walked
        past it.
        """
        tail = ' SELECT * FROM unnest(%s::text[]) ORDER BY 1 ON CONFLICT(symbol) DO NOTHING'
        for label, source in (
            ("f-string", f'SQL = f"INSERT INTO {{SCHEMA}}.instruments(symbol){tail}"\n'),
            ("f-string, no column list", f'SQL = f"INSERT INTO {{SCHEMA}}.instruments{tail}"\n'),
            ("f-string, quoted identifier",
             f'SQL = f\'INSERT INTO {{SCHEMA}}."instruments"(symbol){tail}\'\n'),
            ("concatenation", f'SQL = "INSERT INTO " + SCHEMA + ".instruments(symbol){tail}"\n'),
            ("percent format", f'SQL = "INSERT INTO %s.instruments(symbol){tail}" % SCHEMA\n'),
            ("percent format, named",
             f'SQL = "INSERT INTO %(schema)s.instruments(symbol){tail}" % {{"schema": S}}\n'),
            ("str.format", f'SQL = "INSERT INTO {{}}.instruments(symbol){tail}".format(SCHEMA)\n'),
            ("str.format, named",
             f'SQL = "INSERT INTO {{schema}}.instruments(symbol){tail}".format(schema=S)\n'),
        ):
            with self.subTest(label):
                self.assertEqual(len(_runtime_table_statements_in(*_probe(source))), 1, source)

    def test_a_literal_statement_is_not_mistaken_for_an_interpolated_one(self) -> None:
        """The check must not fire on what ``NEEDLE`` can already see.

        Every psycopg statement in this repository is full of ``%s``
        parameter markers and many are f-strings for unrelated reasons; a
        guard that reported those would be turned off within a week.
        """
        for label, source in (
            # ``%s`` parameters in a plain literal: not interpolation at all.
            ("parameter markers",
             'SQL = "INSERT INTO quant.instruments(symbol) VALUES(%s) ON CONFLICT DO NOTHING"\n'),
            # The schema is spelled out; the f-string interpolates something else.
            ("literal table in an f-string",
             'SQL = f"INSERT INTO quant.instruments(symbol) VALUES({value})"\n'),
            ("literal quoted table in an f-string",
             'SQL = f\'INSERT INTO quant."instruments"(symbol) VALUES({value})\'\n'),
            # A migration's foreign key inside an interpolated DDL statement.
            ("references clause",
             'SQL = f"CREATE TABLE {SCHEMA}.child(symbol text REFERENCES quant.instruments(symbol))"\n'),
            # A READ is not a writer; this guard is about the write path.
            ("interpolated read", 'SQL = f"SELECT symbol FROM {SCHEMA}.instruments"\n'),
            # A different table that merely starts the same way.
            ("sibling table", 'SQL = f"INSERT INTO {SCHEMA}.instruments_cold(symbol) VALUES(%s)"\n'),
            # ``%s`` in a template that formats something else entirely.
            ("unrelated percent format", 'MESSAGE = "wrote %s rows to quant.instruments" % count\n'),
        ):
            with self.subTest(label):
                self.assertEqual(_runtime_table_statements_in(*_probe(source)), [], source)

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

    def test_every_instrument_writer_runs_through_the_lock_retry_helper(self) -> None:
        """No exemption, the registry included: its two constants are routed too."""
        trees, _failures = _parsed()
        offenders = _unrouted_writers_in(trees)
        self.assertEqual(offenders, [], "\n".join([
            "",
            "These quant.instruments writers execute their statement directly.  Pass it",
            "to app/instrument_lock_retry.execute_instrument_write(connection, SQL, params,",
            "writer=...) instead -- the bounded savepoint/lock_timeout retry is what keeps",
            "the owner from being the victim of a cycle with a per-row writer it does",
            "not control.  There is no allow-list.",
            *offenders,
        ]))
        registry_tree = dict(trees)[REGISTRY]
        routed = {
            _referenced_name(_helper_query(call))
            for call in ast.walk(registry_tree) if _helper_query(call) is not None
        }
        self.assertLessEqual({"ENSURE_INSTRUMENTS_SQL", "NAMED_INSTRUMENTS_SQL"}, routed)
        # And the walk really found routed writers outside the registry, in
        # both app/ and scripts/, so a green result is not a vacuous one.
        routed_files = {
            path for path, tree in trees
            if any(isinstance(call, ast.Call) and _helper_query(call) is not None for call in ast.walk(tree))
        }
        self.assertIn(REPO_ROOT / "quant-service" / "app" / "tushare_normalization.py", routed_files)
        self.assertIn(REPO_ROOT / "scripts" / "import-adjusted-research-bars.py", routed_files)

    def test_the_routing_check_rejects_each_way_around_the_helper(self) -> None:
        statement = STATEMENT_PROBE
        for label, source in (
            ("bare connection.execute", f'def f(c):\n    c.execute("{statement}", (x,))\n'),
            ("cursor.execute", f'def f(cur):\n    cur.execute("{statement}")\n'),
            ("executemany", f'def f(c):\n    c.executemany("{statement}", rows)\n'),
            ("local variable",
             f'def f(c):\n    sql = "{statement}"\n    execute_instrument_write(c, sql, writer="x")\n'),
            ("literal as a parameter, not the query",
             f'def f(c):\n    execute_instrument_write(c, other, ("{statement}",), writer="x")\n'),
            ("constant executed directly", f'SQL = "{statement}"\ndef f(c):\n    c.execute(SQL, (x,))\n'),
            ("constant never routed", f'SQL = "{statement}"\n'),
            ("constant routed once, executed directly once",
             f'SQL = "{statement}"\ndef f(c):\n    execute_instrument_write(c, SQL, writer="x")\n'
             '    c.execute(SQL)\n'),
        ):
            with self.subTest(label):
                self.assertTrue(_unrouted_writers_in([_probe(source)]), source)

    def test_the_routing_check_accepts_the_routed_shapes(self) -> None:
        statement = STATEMENT_PROBE
        for label, source in (
            ("routed literal",
             f'def f(c):\n    execute_instrument_write(c, "{statement}", (x,), writer="x")\n'),
            ("routed literal via module",
             f'def f(c):\n    m.execute_instrument_write(c, "{statement}", writer="x")\n'),
            ("routed keyword query",
             f'def f(c):\n    execute_instrument_write(c, query="{statement}", writer="x")\n'),
            ("routed constant",
             f'SQL = "{statement}"\ndef f(c):\n    execute_instrument_write(c, SQL, writer="x")\n'),
            ("docstring", f'def f(c):\n    """Replaces {statement}."""\n'),
        ):
            with self.subTest(label):
                self.assertEqual(_unrouted_writers_in([_probe(source)]), [], source)

    def test_an_imported_constant_executed_directly_elsewhere_is_caught(self) -> None:
        """The constant's own module routes it; a second module must not bypass that."""
        statement = STATEMENT_PROBE
        owner = _probe(
            f'SQL = "{statement}"\ndef f(c):\n    execute_instrument_write(c, SQL, writer="x")\n', "owner.py",
        )
        importer = _probe("from owner import SQL\ndef g(c):\n    c.execute(SQL, (1,))\n", "importer.py")
        qualified = _probe("import owner\ndef g(c):\n    c.execute(owner.SQL)\n", "qualified.py")
        self.assertEqual(_unrouted_writers_in([owner]), [])
        self.assertTrue(_unrouted_writers_in([owner, importer]))
        self.assertTrue(_unrouted_writers_in([owner, qualified]))

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
