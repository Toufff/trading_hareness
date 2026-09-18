"""Architecture guard for the owner database's hot/cold storage tiers (spec S6).

``scripts/database-storage-tiers.py`` moves rows out of five evidence tables
into ``quant.<table>_cold`` twins on the HDD.  Three things must stay true for
that to be safe, and none of them is visible from the script alone:

* every tiered table really exists, and its policy column really is a
  ``timestamptz`` of that table -- a renamed column or a typo would make the
  job either move nothing for a year or compare a cutoff against the wrong
  clock;
* no application code ever reads a ``_cold`` twin or an ``_all`` view.  The
  twins carry no foreign keys and no triggers, so a router that learned to read
  them would quietly bypass every referential guarantee the hot table has;
* ``docs/OWNER_DATABASE_STORAGE.md`` lists exactly the tables the script acts
  on.  The document is what the next agent and the operator read before
  touching the layout; a policy the document does not mention is a table
  nobody knows is being drained.

The script is imported by path and every psycopg import inside it is lazy, so
this test needs neither a database nor a driver.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

from app.database import PLATFORM_SCHEMA_SQL, SCHEMA_SQL

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "database-storage-tiers.py"
DOC = REPO_ROOT / "docs" / "OWNER_DATABASE_STORAGE.md"
APP_DIR = Path(__file__).resolve().parents[1] / "app"
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# The document marks the two authoritative tables with comments so this guard
# reads the same rows a human reads, instead of guessing which markdown table
# is the policy one.
TIER_TABLE_MARKERS = ("<!-- tier-policy-table:begin -->", "<!-- tier-policy-table:end -->")
WHOLE_TABLE_MARKERS = ("<!-- whole-table-cold:begin -->", "<!-- whole-table-cold:end -->")


def load_module():
    spec = importlib.util.spec_from_file_location("database_storage_tiers_policy", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # @dataclass resolves its annotations through sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tiers = load_module()


def _sql_corpus() -> str:
    """The frozen legacy DDL plus every migration's source text.

    Migrations in this repository create these tables with literal
    ``op.execute("CREATE TABLE ...")`` SQL, so reading the files is enough and
    avoids importing Alembic modules purely to inspect a column type.
    """
    parts = [SCHEMA_SQL, PLATFORM_SCHEMA_SQL]
    parts.extend(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.py")))
    return "\n".join(parts)


SQL_CORPUS = _sql_corpus()


def create_table_body(qualified: str) -> str | None:
    """The column list of ``CREATE TABLE [IF NOT EXISTS] <qualified> ( ... )``.

    Parentheses are matched by depth rather than by a regex: the bodies contain
    ``numeric(18,6)``, ``UNIQUE(a,b)`` and ``CHECK (...)``, and a lazy regex
    would stop at the first inner ``)``.
    """
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(qualified) + r"\s*\(",
        re.IGNORECASE,
    )
    match = pattern.search(SQL_CORPUS)
    if match is None:
        return None
    depth = 1
    start = match.end()
    for index in range(start, len(SQL_CORPUS)):
        char = SQL_CORPUS[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return SQL_CORPUS[start:index]
    return None


def declares_timestamptz(qualified: str, column: str) -> bool:
    """``column timestamptz`` in the table body, or added by a later migration."""
    body = create_table_body(qualified)
    if body is not None:
        inline = re.compile(rf"(?:^|,)\s*{re.escape(column)}\s+timestamptz\b", re.IGNORECASE)
        if inline.search(body):
            return True
    added = re.compile(
        r"ALTER\s+TABLE\s+" + re.escape(qualified) + r"\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        + re.escape(column) + r"\s+timestamptz\b",
        re.IGNORECASE,
    )
    return bool(added.search(SQL_CORPUS))


def _marked_rows(text: str, markers: tuple[str, str]) -> list[list[str]]:
    begin, end = markers
    start = text.find(begin)
    stop = text.find(end)
    if start < 0 or stop < 0 or stop < start:
        raise AssertionError(f"{DOC.name} is missing the {begin} / {end} markers")
    rows = []
    body = False
    for line in text[start + len(begin):stop].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            body = True  # the header separator; everything before it is the header
            continue
        if body:
            rows.append(cells)
    if not rows:
        raise AssertionError(f"{DOC.name} has no data rows between {begin} and {end}")
    return rows


def _unquote(cell: str) -> str:
    return cell.strip().strip("`").strip()


class TieredTablesExistInTheSchemaTest(unittest.TestCase):
    """A policy may only name a table and a column the schema actually has."""

    def test_every_tiered_table_is_created_by_the_frozen_ddl_or_a_migration(self):
        for policy in tiers.TIER_POLICY:
            with self.subTest(table=policy.qualified):
                self.assertIsNotNone(
                    create_table_body(policy.qualified),
                    f"{policy.qualified} is in TIER_POLICY but no CREATE TABLE for it exists in the "
                    "frozen DDL or in migrations/versions",
                )

    def test_every_policy_column_is_a_timestamptz_of_its_table(self):
        for policy in tiers.TIER_POLICY:
            with self.subTest(table=policy.qualified, column=policy.column):
                self.assertTrue(
                    declares_timestamptz(policy.qualified, policy.column),
                    f"{policy.qualified}.{policy.column} is the tier cutoff column but is not "
                    "declared timestamptz; a naive or non-existent column would move the wrong rows",
                )

    def test_whole_table_cold_placements_exist_too(self):
        for qualified in tiers.WHOLE_TABLE_COLD:
            with self.subTest(table=qualified):
                self.assertIsNotNone(
                    create_table_body(qualified),
                    f"{qualified} is placed wholly in the cold tablespace but is not created anywhere",
                )

    def test_twin_and_view_names_are_not_themselves_real_tables(self):
        # A real quant.<table>_cold in the DDL would collide with the twin the
        # install step creates, and the collision would only show up at 06:00.
        for policy in tiers.TIER_POLICY:
            with self.subTest(table=policy.qualified):
                self.assertIsNone(create_table_body(policy.cold_table))
                self.assertIsNone(create_table_body(policy.all_view))


def unique_index_statements(qualified: str) -> list[str]:
    """Every ``CREATE UNIQUE INDEX ... ON <qualified> ...`` in the DDL corpus.

    Terminated by the first ``;`` or blank line, which is how these statements
    are written in ``app/database.py`` and in the migrations.
    """
    pattern = re.compile(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*?\bON\s+" + re.escape(qualified) + r"\b[^;]*",
        re.IGNORECASE | re.DOTALL,
    )
    return [" ".join(match.group(0).split()) for match in pattern.finditer(SQL_CORPUS)]


class TieredTablesHaveNoUniqueIndexTheMoveCannotReasonAboutTest(unittest.TestCase):
    """A partial or expression unique index would make the move drop rows silently.

    ``unique_key_columns`` in the script skips unique indexes with a predicate
    (``WHERE ...``) or an expression key, because the conflict scan joins the
    batch to the twin on a *column list* and neither of those is one.  But
    ``CREATE TABLE ... (LIKE <hot> INCLUDING INDEXES)`` copies them to the twin
    verbatim, so a hot row colliding on such an index would be swallowed by
    ``ON CONFLICT DO NOTHING`` and then counted as a benign
    ``already_in_cold_rows``.

    ``_move_table`` refuses the table at run time with
    ``unsupported_unique_index``.  That refusal is correct but it is a 06:00
    surprise; this guard makes the migration that would introduce one fail in
    the release gate instead.  None of the five tiered tables has one today (all
    nine of their unique indexes are plain), so this is a fence, not a fix.
    """

    def test_no_tiered_table_declares_a_partial_or_expression_unique_index(self):
        offenders = []
        for policy in tiers.TIER_POLICY:
            for statement in unique_index_statements(policy.qualified):
                # "ON quant.t (a, b)" is fine; "ON quant.t (lower(a))" and
                # "... WHERE deleted_at IS NULL" are not.
                key = re.search(r"\(\s*(.*?)\s*\)\s*(WHERE\b.*)?$", statement, re.IGNORECASE)
                predicate = re.search(r"\)\s*WHERE\b", statement, re.IGNORECASE)
                expression = bool(key and "(" in key.group(1))
                if predicate or expression:
                    offenders.append(f"{policy.qualified}: {statement}")
        self.assertEqual(
            offenders,
            [],
            "a partial or expression UNIQUE index on a tiered table makes the storage-tier move "
            "refuse that table (status unsupported_unique_index): the conflict scan joins on column "
            "lists only. Give the table a plain unique key, or teach conflict_scan_sql the new shape.",
        )

    def test_the_inline_unique_constraints_of_the_tiered_tables_are_plain_column_lists(self):
        # UNIQUE(...) written inside CREATE TABLE is the common form here and is
        # copied to the twin the same way.
        offenders = []
        for policy in tiers.TIER_POLICY:
            body = create_table_body(policy.qualified) or ""
            for match in re.finditer(r"\bUNIQUE\s*\(([^)]*)\)", body, re.IGNORECASE):
                columns = match.group(1)
                if not re.fullmatch(r"[\s\w,]*", columns):
                    offenders.append(f"{policy.qualified}: UNIQUE({columns})")
        self.assertEqual(offenders, [], "an expression inside an inline UNIQUE(...) has the same effect")

    def test_the_guard_would_actually_catch_one(self):
        # The regexes above are the whole test; pin them against samples so a
        # future rewrite cannot quietly turn the guard into a no-op.
        partial = "CREATE UNIQUE INDEX x ON quant.raw_market_observations (symbol) WHERE symbol IS NOT NULL"
        expression = "CREATE UNIQUE INDEX y ON quant.raw_market_observations (lower(symbol))"
        plain = "CREATE UNIQUE INDEX z ON quant.raw_market_observations (symbol, available_at)"
        self.assertIsNotNone(re.search(r"\)\s*WHERE\b", partial, re.IGNORECASE))
        self.assertIsNone(re.search(r"\)\s*WHERE\b", plain, re.IGNORECASE))
        for statement, expected in ((expression, True), (plain, False)):
            key = re.search(r"\(\s*(.*?)\s*\)\s*(WHERE\b.*)?$", statement, re.IGNORECASE)
            self.assertEqual("(" in key.group(1), expected, statement)


class ApplicationCodeNeverReadsTheColdTierTest(unittest.TestCase):
    """The twins have no foreign keys and no triggers; only operations reads them."""

    def test_no_app_module_references_a_cold_twin_or_an_all_view(self):
        forbidden = []
        for policy in tiers.TIER_POLICY:
            forbidden.append(policy.cold_table)
            forbidden.append(policy.cold_table.split(".", 1)[1])
            forbidden.append(policy.all_view)
            forbidden.append(policy.all_view.split(".", 1)[1])
        offenders = []
        for path in sorted(APP_DIR.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                if re.search(rf"\b{re.escape(name)}\b", text):
                    offenders.append(f"{path.relative_to(APP_DIR.parent)}: {name}")
        self.assertEqual(
            offenders,
            [],
            "application code must read the hot table only; the cold twin carries no foreign keys "
            "and no triggers, and the _all view is an operations tool (AGENTS.md)",
        )


class DocumentationMatchesThePolicyTest(unittest.TestCase):
    """docs/OWNER_DATABASE_STORAGE.md is the contract the next agent reads."""

    @classmethod
    def setUpClass(cls):
        cls.text = DOC.read_text(encoding="utf-8")

    def test_the_documented_policy_table_lists_exactly_the_scripts_policy(self):
        documented = [
            (_unquote(row[0]), _unquote(row[1]), int(_unquote(row[2])))
            for row in _marked_rows(self.text, TIER_TABLE_MARKERS)
        ]
        self.assertEqual(
            documented,
            [(policy.qualified, policy.column, policy.hot_days) for policy in tiers.TIER_POLICY],
            "the tier policy table in docs/OWNER_DATABASE_STORAGE.md and TIER_POLICY in "
            "scripts/database-storage-tiers.py have drifted apart",
        )

    def test_the_documented_whole_table_cold_list_matches(self):
        documented = tuple(_unquote(row[0]) for row in _marked_rows(self.text, WHOLE_TABLE_MARKERS))
        self.assertEqual(documented, tiers.WHOLE_TABLE_COLD)

    def test_the_document_names_the_budget_and_the_cold_tablespace(self):
        # The three values an operator needs before touching the layout; each is
        # also a default in the script, so a silent change must break here.
        self.assertIn(tiers.COLD_TABLESPACE, self.text)
        self.assertIn("PGDATA_BUDGET_BYTES", self.text)
        self.assertIn("PGDATA_COLD_TABLESPACE_DIR", self.text)


if __name__ == "__main__":
    unittest.main()
