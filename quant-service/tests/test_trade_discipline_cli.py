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
import io
import unittest
from contextlib import contextmanager
from datetime import date, time
from decimal import Decimal
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


class StreamEncodingTests(unittest.TestCase):
    """The receipt carries Chinese instrument names; the console must not mangle them."""

    def test_the_receipt_streams_are_forced_to_utf8(self):
        out = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        cli._utf8_streams(out, err)
        self.assertEqual((out.encoding, err.encoding), ("utf-8", "utf-8"))
        out.write(cli._json({"name": "神奇制药"}))
        out.flush()
        self.assertIn("神奇制药".encode("utf-8"), out.buffer.getvalue())

    def test_a_captured_stream_without_reconfigure_is_left_alone(self):
        captured = io.StringIO()
        cli._utf8_streams(captured)      # must not raise
        captured.write("ok")
        self.assertEqual(captured.getvalue(), "ok")

    def test_main_reconfigures_before_parsing(self):
        source = SCRIPT.read_text(encoding="utf-8")
        body = source.split("def main(argv=None):", 1)[1]
        self.assertLess(body.index("_utf8_streams(sys.stdout, sys.stderr)"), body.index("parse_args"))


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


class TradeRowTests(unittest.TestCase):
    """A real fill must never vanish between the query and the verdict."""

    ROWS = [
        {"record_id": "r-1", "trade_date": date(2026, 9, 21), "trade_time": time(14, 50),
         "symbol": "600613.SH", "name": "神奇制药", "side": "sell", "quantity": 3000,
         "price": Decimal("7.62")},
        {"record_id": "r-2", "trade_date": date(2026, 9, 21), "trade_time": time(14, 55),
         "symbol": "600613.SH", "name": "神奇制药", "side": "sell", "quantity": 2800, "price": None},
        {"record_id": "r-3", "trade_date": date(2026, 9, 22), "trade_time": time(9, 40),
         "symbol": "600000.SH", "name": "浦发银行", "side": "buy", "quantity": 1000,
         "price": Decimal("0")},
    ]

    def connection(self, rows):
        class Rows:
            def fetchall(self_inner):
                return rows

        class Connection:
            def execute(self_inner, _sql, _params=None):
                return Rows()
        return Connection()

    def test_a_fill_without_a_usable_price_is_reported_rather_than_dropped(self):
        priced, unpriced = cli._trade_rows(self.connection(self.ROWS[:2]), "citics-primary",
                                           "600613.SH", date(2026, 9, 18), date(2026, 9, 30))
        self.assertEqual([row["trade_record_id"] for row in priced], ["r-1"])
        self.assertEqual([row["trade_record_id"] for row in unpriced], ["r-2"])
        self.assertEqual(unpriced[0]["quantity"], 2800)
        self.assertIsNone(unpriced[0]["price"])

    def test_a_zero_price_counts_as_missing_and_is_surfaced_too(self):
        _, unpriced = cli._trade_rows(self.connection(self.ROWS[2:]), "citics-primary",
                                      "600000.SH", date(2026, 9, 18), date(2026, 9, 30))
        self.assertEqual([row["trade_record_id"] for row in unpriced], ["r-3"])

    def test_a_fill_in_a_symbol_no_plan_covers_is_listed_as_unplanned(self):
        fills = cli._unplanned_fills(self.connection(self.ROWS), "citics-primary",
                                     {"600613.SH"}, date(2026, 9, 18), date(2026, 9, 30))
        self.assertEqual([row["trade_record_id"] for row in fills], ["r-3"])
        self.assertEqual(fills[0]["verdict"], "unplanned")
        self.assertIn("600000.SH", fills[0]["notes"])

    def test_the_reconcile_receipt_always_carries_the_unplanned_channel(self):
        receipt = cli.command_reconcile(
            cli.build_parser().parse_args(["reconcile", "--dry-run", "--date", "2026-09-21"]),
            FakeDatabase())
        self.assertEqual(receipt["unplanned_fills"], [])


class GenerationWarningTests(unittest.TestCase):
    """A plan dated the previous session on an open day is valid but must not pass unnoticed."""

    def test_a_stale_day_evidence_ref_becomes_a_plain_words_receipt_warning(self):
        from app.trade_discipline import inputs as inputs_module
        from app.trade_discipline.generator import generate
        from test_trade_discipline_core import shenqi_inputs

        fresh = generate(shenqi_inputs())
        self.assertEqual(cli._generation_warnings(fresh, inputs_module), [])
        stale = generate(shenqi_inputs(evidence_refs=["bars_basis:settled_only",
                                                      "bars_stale_day:2026-09-21:last_settled:2026-09-18"]))
        warnings = cli._generation_warnings(stale, inputs_module)
        self.assertEqual(len(warnings), 1)
        self.assertIn("stale_day", warnings[0])
        self.assertIn("2026-09-18", warnings[0])
        self.assertIn("bars_stale_day:2026-09-21:last_settled:2026-09-18", warnings[0])


class _FakePlan:
    """Only the attributes ``command_generate``'s persist block reads."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.name = symbol
        self.stage = "hold"
        self.plan_kind = "position"
        self.status = "active"
        self.plan_key = f"key:{symbol}"
        self.trading_date = date(2026, 9, 18)
        self.valid_until = date(2026, 9, 19)
        self.quality = []
        self.sizing = None
        self.lines = []
        self.inputs_hash = f"hash:{symbol}"
        self.evidence_refs = []
        self.supersedes_plan_id = None

    def model_dump(self, **_kwargs):
        return {"symbol": self.symbol}


class GenerateLockOrderTests(unittest.TestCase):
    """The persist loop's order is a lock order, and nothing else pins it.

    Every ``persist_plan`` registers its instrument through
    ``ensure_named_instruments`` -- ``ON CONFLICT DO UPDATE``, the strongest
    lock class on ``quant.instruments`` -- and all of these calls share ONE
    transaction, so two operators running this command over overlapping
    holdings in different ``--symbol`` orders is the cycle
    ``app/instrument_registry.py`` documents.  ``persist_plan`` is
    single-symbol, so every repository-level test passes either way, and the
    repository-wide guard cannot see the sort at all: its loop check is
    lexical and the SQL literal lives in ``app/instrument_registry.py``, not
    here.  Deleting the ``sorted(...)`` in ``command_generate`` therefore
    left the whole suite green until this test existed.
    """

    def _run(self, symbols):
        from unittest.mock import patch

        from app.trade_discipline import inputs as inputs_module
        from app.trade_discipline import repository

        persisted = []

        def persist_plan(_connection, plan, **_kwargs):
            persisted.append(plan.symbol)
            return {"plan_id": f"id:{plan.symbol}", "status": "active", "content_hash": "c",
                    "plan": {"plan_key": plan.plan_key, "content_hash": "c", "status": "active"}}

        async def collect(*_args, **kwargs):
            symbol = kwargs["symbol"]

            class _Inputs:
                def model_dump(self, **_k):
                    return {"symbol": symbol}

            return _Inputs()

        args = cli.build_parser().parse_args(
            ["generate", *[token for symbol in symbols for token in ("--symbol", symbol)],
             "--as-of", "2026-09-18T15:30"])
        with patch.object(inputs_module, "collect", collect), \
             patch.object(inputs_module, "inputs_hash", lambda _payload: "run-hash"), \
             patch("app.trade_discipline.generator.generate",
                   side_effect=lambda generation_inputs: _FakePlan(generation_inputs.model_dump()["symbol"])), \
             patch("app.trade_discipline.report.write_report", return_value={}), \
             patch.object(repository, "persist_generation_run", lambda *_a, **_k: None), \
             patch.object(repository, "persist_plan", persist_plan):
            receipt = cli.command_generate(args, FakeDatabase())
        return receipt, persisted

    def test_plans_are_persisted_in_ascending_symbol_order(self):
        receipt, persisted = self._run(["600613.SH", "000001.SZ"])
        self.assertEqual(persisted, ["000001.SZ", "600613.SH"])
        self.assertEqual(receipt["errors"], [])

    def test_the_reported_order_still_follows_the_symbols_as_typed(self):
        """The sort is a lock order, not an output order.

        The receipts are mutated in place, so the operator's JSON -- and the
        report files already written above the transaction -- must still read
        in the order ``--symbol`` was given.
        """
        receipt, _persisted = self._run(["600613.SH", "000001.SZ"])
        self.assertEqual([plan["symbol"] for plan in receipt["plans"]], ["600613.SH", "000001.SZ"])
        self.assertEqual(receipt["symbols"], ["600613.SH", "000001.SZ"])
        self.assertEqual([plan["plan_id"] for plan in receipt["plans"]],
                         ["id:600613.SH", "id:000001.SZ"])

    def test_the_order_is_the_same_whichever_way_the_operator_types_it(self):
        """Two operators, opposite argument orders, one lock order."""
        _forward_receipt, forward = self._run(["000001.SZ", "300750.SZ", "600613.SH"])
        _reverse_receipt, reverse = self._run(["600613.SH", "300750.SZ", "000001.SZ"])
        self.assertEqual(forward, ["000001.SZ", "300750.SZ", "600613.SH"])
        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
