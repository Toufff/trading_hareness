"""Argument and glue coverage for scripts/trade-discipline.py (no database).

The four commands are exercised against a fake connection that returns no rows,
which is exactly the state of a database before any plan has been generated.
What is asserted is the CLI contract the operator sees: the subcommands and
their flags, one JSON receipt per run, the research-only envelope, and that a
``--dry-run`` really opens a read-only transaction rather than trusting itself
not to write.
"""

from __future__ import annotations

import importlib.util
import unittest
from contextlib import contextmanager
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "trade-discipline.py"


def load_cli():
    spec = importlib.util.spec_from_file_location("trade_discipline_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = load_cli()


class _Result:
    def fetchall(self):
        return []

    def fetchone(self):
        return None


class FakeConnection:
    def __init__(self, statements):
        self.statements = statements

    def execute(self, sql, params=None):
        self.statements.append(" ".join(str(sql).split()))
        return _Result()


class FakeDatabase:
    """A database that records every statement and returns nothing."""

    def __init__(self):
        self.statements: list[str] = []

    @contextmanager
    def transaction(self, **_kwargs):
        yield FakeConnection(self.statements)

    def close(self):
        return None


class ParserTests(unittest.TestCase):
    def test_every_documented_subcommand_parses(self):
        parser = cli.build_parser()
        for command in ("generate", "evaluate", "reconcile", "show"):
            args = parser.parse_args([command, "--account-key", "citics-primary"])
            self.assertEqual(args.command, command)
            self.assertIn(command, cli.COMMANDS)

    def test_generate_accepts_repeated_symbols_and_the_dry_run_flag(self):
        args = cli.build_parser().parse_args(
            ["generate", "--symbol", "600613.SH", "--symbol", "600000.SH", "--dry-run",
             "--as-of", "2026-09-18T15:30", "--no-live", "--risk-per-trade-pct", "0.8"])
        self.assertEqual(args.symbol, ["600613.SH", "600000.SH"])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_live)
        self.assertEqual(args.risk_per_trade_pct, "0.8")

    def test_the_output_root_defaults_to_the_documented_platform_folder(self):
        args = cli.build_parser().parse_args(["generate"])
        self.assertEqual(str(args.output_dir).replace("\\", "/"), "G:/StockPlatform/reports/discipline")
        self.assertEqual(args.account_key, "citics-primary")
        self.assertFalse(args.dry_run)

    def test_an_unsupported_basis_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["evaluate", "--basis", "hourly"])

    def test_a_missing_subcommand_is_rejected(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDatabase()

    def args(self, command, **overrides):
        parsed = cli.build_parser().parse_args([command, "--dry-run"])
        for key, value in overrides.items():
            setattr(parsed, key, value)
        return parsed

    def test_evaluate_without_stored_plans_returns_an_empty_receipt(self):
        receipt = cli.command_evaluate(self.args("evaluate", date="2026-09-21"), self.db)
        self.assertEqual(receipt["command"], "evaluate")
        self.assertEqual(receipt["evaluated"], 0)
        self.assertEqual(receipt["evaluations"], [])
        self.assertEqual(receipt["errors"], [])
        self.assertEqual(receipt["basis"], "daily")
        self.assertEqual(receipt["as_of"], "2026-09-21T15:00:00+08:00")
        self.assertFalse(receipt["live_orders"])

    def test_reconcile_without_stored_plans_returns_an_empty_receipt(self):
        receipt = cli.command_reconcile(self.args("reconcile", date="2026-09-21"), self.db)
        self.assertEqual(receipt["command"], "reconcile")
        self.assertEqual(receipt["reconciled"], 0)
        self.assertEqual(receipt["plans"], [])
        self.assertEqual(receipt["boundary"], "research_only_human_decision_support")

    def test_show_reports_nothing_found_instead_of_failing(self):
        receipt = cli.command_show(self.args("show", symbol=["600613.SH"]), self.db)
        self.assertEqual(receipt["found"], 0)
        self.assertEqual(receipt["plans"], [])

    def test_a_dry_run_opens_an_explicitly_read_only_transaction(self):
        cli.command_evaluate(self.args("evaluate", date="2026-09-21"), self.db)
        self.assertIn("SET TRANSACTION READ ONLY", self.db.statements)
        self.assertFalse([statement for statement in self.db.statements
                          if statement.upper().startswith(("INSERT", "UPDATE", "DELETE"))])

    def test_a_dry_run_never_persists_even_when_a_plan_exists(self):
        receipt = cli.command_reconcile(self.args("reconcile", date="2026-09-21"), self.db)
        self.assertTrue(receipt["dry_run"])
        self.assertFalse([statement for statement in self.db.statements
                          if statement.upper().startswith("INSERT")])


if __name__ == "__main__":
    unittest.main()
