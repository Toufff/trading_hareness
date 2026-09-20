import asyncio
import importlib.util
import json
import os
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agent_paper.baseline import replay_fills
from app.agent_paper.context import compact_minutes, context_part_chars, summarize_daily_bars, summarize_minutes
from app.agent_paper.model import ModelFailure, ModelResult, parse_cli_result, parse_codex_stream
from app.agent_paper.rules import apply_fill, equity, limit_crossed, limit_prices, normalize_order, tradability_reasons, walk_book
from app.agent_paper.runner import decision_due, in_session

SH = ZoneInfo("Asia/Shanghai")


def book(price="7.00", pre_close="7.00", bids=(("6.99", 10),), asks=(("7.01", 5), ("7.02", 10))):
    return {"ts_code": "600664.SH", "name": "哈药股份", "price": price, "pre_close": pre_close,
            "bids": [{"price": p, "size": s} for p, s in bids], "asks": [{"price": p, "size": s} for p, s in asks]}


class AgentPaperRuleTests(unittest.TestCase):
    def test_market_buy_walks_depth_and_pays_the_spread(self):
        filled, price = walk_book("buy", 1000, book(), None)
        self.assertEqual(filled, 1000)
        self.assertEqual(price, (Decimal("7.01") * 500 + Decimal("7.02") * 500) / 1000)

    def test_limit_stops_at_price_and_rounds_partial_lot_down(self):
        filled, price = walk_book("buy", 800, book(asks=(("7.01", 5), ("7.03", 10))), Decimal("7.02"))
        self.assertEqual((filled, price), (500, Decimal("7.01")))
        filled, _ = walk_book("sell", 1000, book(bids=(("6.99", 2.5),)), None)
        self.assertEqual(filled, 200)

    def test_limit_up_without_asks_cannot_buy_and_t_plus_one_blocks_sell(self):
        sealed = book(price="7.70", pre_close="7.00", asks=())
        self.assertEqual(tradability_reasons("buy", sealed, sellable=0, quantity=100), ["limit_up_or_no_ask_depth"])
        self.assertIn("t_plus_one_or_insufficient_sellable_quantity",
                      tradability_reasons("sell", book(), sellable=0, quantity=100))
        self.assertEqual(tradability_reasons("buy", None, sellable=0, quantity=100), ["no_live_quote"])

    def test_limit_prices_follow_board_and_st_bands(self):
        self.assertEqual(limit_prices("600664.SH", "哈药股份", "7.12"), (Decimal("7.83"), Decimal("6.41")))
        self.assertEqual(limit_prices("300001.SZ", "特锐德", "10"), (Decimal("12.00"), Decimal("8.00")))
        self.assertEqual(limit_prices("600001.SH", "*ST某某", "10"), (Decimal("10.50"), Decimal("9.50")))

    def test_order_validation_rejects_instead_of_repairing(self):
        positions = {"600664.SH": {"quantity": 2350}}
        self.assertEqual(normalize_order({"action": "buy", "symbol": "600664.SH", "quantity": 150, "reason": "x"},
                                         positions=positions, open_order_ids=set()).reasons, ["not_board_lot"])
        odd_exit = normalize_order({"action": "sell", "symbol": "600664.SH", "quantity": 2350, "order_type": "market",
                                    "limit_price": 7.5, "reason": "清仓"}, positions=positions, open_order_ids=set())
        self.assertEqual(odd_exit.order["quantity"], 2350)
        self.assertIsNone(odd_exit.order["limit_price"])
        missing = normalize_order({"action": "sell", "symbol": "600664", "quantity": 100, "order_type": "limit"},
                                  positions=positions, open_order_ids=set())
        self.assertEqual(set(missing.reasons), {"invalid_symbol", "limit_price_required", "reason_required"})
        self.assertEqual(normalize_order({"action": "cancel", "order_id": "nope", "reason": "x"}, positions={},
                                         open_order_ids={"a"}).reasons, ["cancel_unknown_order"])

    def test_fills_update_cash_cost_and_realized_pnl(self):
        cash, position = apply_fill(Decimal("10000"), None, side="buy", quantity=1000, price=Decimal("7"), fees=Decimal("5"))
        self.assertEqual(cash, Decimal("2995"))
        self.assertEqual(position["average_cost"], Decimal("7.005"))
        with self.assertRaisesRegex(ValueError, "insufficient_sellable"):
            apply_fill(cash, position, side="sell", quantity=100, price=Decimal("7.2"), fees=Decimal("5"))
        position["sellable_quantity"] = 1000
        cash, position = apply_fill(cash, position, side="sell", quantity=1000, price=Decimal("7.2"), fees=Decimal("12.2"))
        self.assertEqual(cash, Decimal("10182.8"))
        self.assertEqual(position["realized_pnl"], Decimal("182.8"))

    def test_resting_limit_needs_a_trade_through(self):
        self.assertTrue(limit_crossed("buy", Decimal("7.00"), low=Decimal("6.99"), high=Decimal("7.2")))
        self.assertFalse(limit_crossed("sell", Decimal("7.50"), low=None, high=Decimal("7.49")))

    def test_equity_marks_unpriced_positions_at_cost_and_reports_them(self):
        total, value, missing = equity(Decimal("100"), [{"symbol": "A", "quantity": 100, "average_cost": "2"},
                                                        {"symbol": "B", "quantity": 100, "average_cost": "3"}], {"A": Decimal("2.5")})
        self.assertEqual((total, value, missing), (Decimal("650"), Decimal("550"), ["B"]))

    def test_replay_estimates_missing_broker_fees(self):
        cash, book_rows = replay_fills(Decimal("2936.80"), {"600664.SH": {"symbol": "600664.SH", "quantity": 2000,
                                                                          "average_cost": Decimal("6.8842")}},
                                       [{"symbol": "600664.SH", "side": "buy", "quantity": "300", "price": "7.36",
                                         "commission": "0", "stamp_duty": "0"}])
        self.assertEqual(cash, Decimal("723.80"))
        self.assertEqual(book_rows["600664.SH"]["quantity"], 2300)

    def test_replay_keeps_same_day_buys_unsellable(self):
        _, rows = replay_fills(Decimal("10000"), {"A": {"symbol": "A", "quantity": 1000, "sellable_quantity": 1000,
                                                        "average_cost": Decimal("5")}},
                               [{"symbol": "A", "side": "buy", "quantity": 500, "price": "5"},
                                {"symbol": "A", "side": "sell", "quantity": 300, "price": "5"}])
        self.assertEqual((rows["A"]["quantity"], rows["A"]["sellable_quantity"]), (1200, 700))

    def test_close_equity_retries_and_never_substitutes_cost(self):
        from app.agent_paper.runner import Runner

        class Connection:
            def __init__(self):
                self.navs = []

            def execute(self, sql, params=None):
                class Result:
                    def __init__(self, rows):
                        self.rows = rows

                    def fetchall(self):
                        return self.rows

                    def fetchone(self):
                        return self.rows[0] if self.rows else None
                if "FROM quant.agent_paper_positions" in sql:
                    return Result([{"symbol": "A", "quantity": 100, "average_cost": Decimal("9")}])
                if "FROM quant.agent_paper_accounts" in sql:
                    return Result([{"cash": Decimal("100")}])
                if "INSERT INTO quant.agent_paper_nav" in sql:
                    self.navs.append(params)
                    return Result([])
                raise AssertionError(sql)

        connection = Connection()

        class Database:
            @contextmanager
            def transaction(self):
                yield connection

        responses = [{}, {}, {"A": {"price": "10"}}]

        async def fetch(symbols):
            return responses.pop(0)

        async def no_sleep(seconds):
            return None

        runner = Runner(Database(), "acct", model=None, fetch_quotes=fetch)
        at = datetime(2026, 9, 17, 15, 1, tzinfo=SH)
        self.assertEqual(asyncio.run(runner.snapshot_nav(at, attempts=2, sleep=no_sleep))["recorded"], False)
        self.assertEqual(connection.navs, [])
        result = asyncio.run(runner.snapshot_nav(at, attempts=2, sleep=no_sleep))
        self.assertEqual((result["recorded"], result["equity"]), (True, 1100.0))

    def test_session_and_decision_cadence(self):
        at = lambda h, m: datetime(2026, 9, 17, h, m, tzinfo=SH)  # noqa: E731
        self.assertFalse(in_session(at(9, 29)))
        self.assertTrue(in_session(at(13, 0)))
        self.assertTrue(decision_due(at(9, 30), None, 5))
        self.assertFalse(decision_due(at(9, 33), at(9, 30), 5))
        self.assertFalse(decision_due(at(11, 29), at(11, 0), 5))
        self.assertFalse(decision_due(at(14, 58), at(14, 30), 5))

    def test_minute_summary_uses_only_supplied_rows(self):
        rows = [{"time": f"09{30 + i:02d}", "close": 7 + i / 100, "amount": 1e6 * (i + 1), "volume_lot": 100, "vwap": 7.01}
                for i in range(12)]
        summary = summarize_minutes(rows)
        self.assertEqual((summary["high"], summary["high_time"], len(summary["last_10_minutes"])), (7.11, "0941", 10))
        self.assertEqual(summary["five_minute_buckets_time_close_high_low_amount"][0], ["0930", 7.04, 7.04, 7.0, 15.0])

    def test_focus_only_detail_is_compact_and_measurable(self):
        rows = [{"time": f"09{30 + i:02d}", "close": 7 + i / 100, "amount": 1e6, "volume_lot": 100, "vwap": 7.01}
                for i in range(12)]
        compact = compact_minutes(summarize_minutes(rows))
        self.assertEqual((len(compact["last_5_minutes"]), len(compact["recent_5m_buckets"])), (5, 3))
        self.assertNotIn("last_10_minutes", compact)
        daily = [[f"09-{i:02d}", 10, 11, 9, 10 + i / 10, i, 1 + i] for i in range(1, 12)]
        summary = summarize_daily_bars(daily)
        self.assertEqual((summary["sessions"], summary["latest_mmdd_o_h_l_c_pct_amount_yi"]), (11, daily[-1]))
        metrics = context_part_chars({"account": {"cash": 1}, "detail_symbols": {"A": compact}})
        self.assertGreater(metrics["detail_symbols"], metrics["account"])


class AgentPaperModelTests(unittest.TestCase):
    def test_structured_output_and_fenced_text_are_accepted(self):
        output, usage = parse_cli_result(json.dumps({"structured_output": {"orders": []}, "total_cost_usd": 0.4}))
        self.assertEqual((output, usage["total_cost_usd"]), ({"orders": []}, 0.4))
        output, _ = parse_cli_result(json.dumps({"result": "```json\n{\"orders\": [1]}\n```"}))
        self.assertEqual(output, {"orders": [1]})

    def test_backend_selection_defaults_to_claude_cli_and_rejects_unknown(self):
        from app.agent_paper.model import ClaudeCliModel, CodexCliModel, build_model
        self.assertIsInstance(build_model("claude_cli", model="claude-opus-5"), ClaudeCliModel)
        codex = build_model("codex_cli", model="gpt-5.6-sol", reasoning_effort="high")
        self.assertIsInstance(codex, CodexCliModel)
        self.assertEqual(codex.model, "gpt-5.6-sol/high")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            build_model("unknown")

    def test_cli_proxy_applies_only_to_the_cli_environment(self):
        from unittest.mock import patch
        from app.agent_paper.model import ClaudeCliModel
        with patch.dict(os.environ, {"AGENT_PAPER_CLI_PROXY": "http://127.0.0.1:4537"}):
            env = ClaudeCliModel(model="claude-opus-5").environment()
        self.assertEqual((env["HTTPS_PROXY"], env["NO_PROXY"]), ("http://127.0.0.1:4537", "127.0.0.1,localhost"))
        with patch.dict(os.environ, {"AGENT_PAPER_CLI_PROXY": ""}):
            self.assertEqual(ClaudeCliModel(model="m").proxy, "")

    def test_cli_tools_are_limited_to_read_only_web_research(self):
        from unittest.mock import patch
        from app.agent_paper.model import ClaudeCliModel
        with patch.dict(os.environ, {"AGENT_PAPER_TOOLS": "WebSearch,Bash,WebFetch"}):
            command = ClaudeCliModel(model="m").command()
        self.assertEqual(command[command.index("--tools") + 1], "WebSearch,WebFetch")
        self.assertNotIn("Bash", command)
        # No tools still sends `--tools ""`. Dropping the flag does not mean "no tools" - it restores
        # the CLI's full default toolset and its descriptions, measured at 9x the prompt cost.
        with patch.dict(os.environ, {"AGENT_PAPER_TOOLS": ""}):
            command = ClaudeCliModel(model="m").command()
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", command)

    def test_dsh_backend_injects_rules_and_context_without_tool_reads(self):
        from unittest.mock import patch
        from app.agent_paper.model import DshHeadlessModel, build_model
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            with open(os.path.join(kwargs["cwd"], "AGENTS.md"), encoding="utf-8") as stream:
                seen["injected"] = stream.read()
            seen["legacy_files"] = [os.path.exists(os.path.join(kwargs["cwd"], name))
                                    for name in ("context.json", "instructions.md")]

            class Done:
                returncode, stdout, stderr = 0, '推理…\n{"market_view":"v","orders":[],"focus_symbols":[],"notes":""}', "log"
            return Done()

        self.assertIsInstance(build_model("dsh"), DshHeadlessModel)
        with patch("app.agent_paper.model.subprocess.run", side_effect=fake_run):
            result = DshHeadlessModel(binary="dsh").decide('{"now":"t"}')
        self.assertEqual(seen["command"][:3], ["dsh", "--profile", "headless"])
        self.assertIn("--patch", seen["command"])
        self.assertIn('{"now":"t"}', seen["injected"])
        self.assertEqual((seen["legacy_files"], result.output["orders"]), ([False, False], []))

    def test_codex_transport_uses_saved_login_read_only_and_parses_usage(self):
        from unittest.mock import patch
        from app.agent_paper.model import CodexCliModel
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(command=command, env=kwargs["env"], prompt=kwargs["input"])

            class Done:
                returncode, stderr = 0, ""
                stdout = "\n".join([
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text":
                               '{"analysis":"a","market_view":"v","orders":[],"focus_symbols":[],"notes":""}'}}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "cached_input_tokens": 3,
                                                                        "output_tokens": 8}}),
                ])
            return Done()

        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-leak", "CODEX_API_KEY": "must-not-leak"}), \
             patch("app.agent_paper.model.subprocess.run", side_effect=fake_run):
            result = CodexCliModel(model="gpt-5.6-sol", reasoning_effort="high", binary="codex").decide('{"now":"t"}')
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        self.assertNotIn("CODEX_API_KEY", seen["env"])
        self.assertIn("read-only", seen["command"])
        self.assertIn('model_reasoning_effort="high"', seen["command"])
        self.assertIn('{"now":"t"}', seen["prompt"])
        self.assertEqual((result.model, result.usage["input_tokens"]), ("gpt-5.6-sol/high", 12))

    def test_codex_stream_fails_closed_without_a_final_message(self):
        with self.assertRaises(ModelFailure) as caught:
            parse_codex_stream(json.dumps({"type": "turn.failed", "error": {"message": "nope"}}))
        self.assertEqual(caught.exception.code, "codex_failed")

    def test_stream_transcript_keeps_searches_and_result(self):
        from app.agent_paper.model import parse_cli_result, parse_cli_stream
        lines = [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "先看半导体资金方向"},
                                                          {"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {"query": "华天科技 公告"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "x" * 7000}]}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
            {"type": "result", "is_error": False, "structured_output": {"market_view": "v", "orders": []}, "usage": {}},
        ]
        result, transcript = parse_cli_stream("\n".join(json.dumps(line, ensure_ascii=False) for line in lines))
        self.assertEqual(parse_cli_result(result)[0]["market_view"], "v")
        self.assertEqual([e["type"] for e in transcript], ["thinking", "tool_use", "tool_result", "text"])
        self.assertEqual(transcript[0]["text"], "先看半导体资金方向")
        self.assertEqual(transcript[1]["input"], {"query": "华天科技 公告"})
        self.assertIn("truncated 1000 chars", transcript[2]["content"])

    def test_cli_errors_fail_closed(self):
        with self.assertRaises(ModelFailure) as caught:
            parse_cli_result(json.dumps({"is_error": True, "api_error_status": 403, "result": "Request not allowed"}))
        self.assertEqual(caught.exception.code, "cli_error")
        with self.assertRaises(ModelFailure):
            parse_cli_result("not json")


class _StubRunner:
    """Only the surface ``run_day`` touches; every model call fails unless ``fail_until`` says otherwise."""

    def __init__(self, *, fail_rounds, max_consecutive_failures=3):
        self.fail_rounds, self.account_key = fail_rounds, "agent-stub"
        self.decision_minutes, self.max_consecutive_failures = 1, max_consecutive_failures
        self.start_at = datetime(2000, 1, 1, tzinfo=SH)
        self.calls, self.navs, self.ended = 0, 0, False

    @contextmanager
    def _tx(self):
        yield None

    def start_of_day(self, now):
        return False

    def end_of_day(self, now):
        self.ended = True
        return 0

    async def match_open_orders(self, now):
        return []

    async def snapshot_nav(self, now, **kwargs):
        self.navs += 1
        return {"equity": 100.0}

    async def decide(self, now):
        self.calls += 1
        if self.calls in self.fail_rounds:
            return {"status": "model_failed", "error": "cli_error"}
        return {"status": "decided", "orders": []}


class AgentPaperCircuitBreakerTests(unittest.TestCase):
    """An exhausted subscription must not be retried every round for the rest of the day."""

    def _run(self, runner):
        from unittest.mock import patch

        from app.agent_paper import runner as runner_module
        # 09:30 to 15:01 at one simulated minute per pass, so the loop exits without real waiting.
        clock = {"now": datetime(2026, 9, 21, 9, 30, tzinfo=SH)}

        def now_fn():
            return clock["now"]

        async def tick(_seconds):
            clock["now"] += timedelta(minutes=1)

        events = []
        with patch.object(runner_module.repo, "last_decision_at", return_value=None):
            asyncio.run(runner_module.run_day(runner, now_fn=now_fn, sleep=tick, log=events.append))
        return events

    def test_three_consecutive_failures_halt_decisions_but_finish_the_day(self):
        runner = _StubRunner(fail_rounds=range(1, 500))
        events = self._run(runner)
        halted = [e for e in events if e["event"] == "decisions_halted"]
        self.assertEqual(len(halted), 1, "the breaker must announce itself exactly once")
        self.assertEqual(halted[0]["after_consecutive_failures"], 3)
        self.assertEqual(runner.calls, 3, "no model call may happen after the breaker trips")
        # The day still closes normally: positions are untouched and resting orders keep matching.
        day_end = [e for e in events if e["event"] == "day_end"]
        self.assertEqual(len(day_end), 1)
        self.assertEqual(day_end[0]["halted"], "cli_error")
        self.assertTrue(runner.ended)
        self.assertGreater(runner.navs, 1, "NAV snapshots must continue after the halt")

    def test_an_intervening_success_resets_the_counter(self):
        runner = _StubRunner(fail_rounds={1, 2, 4, 5})
        events = self._run(runner)
        self.assertEqual([e["event"] for e in events].count("decisions_halted"), 0)
        self.assertGreater(runner.calls, 6, "two failures then a success must not stop the day")


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260917_0102_agent_paper_trading.py"
    spec = importlib.util.spec_from_file_location("agent_paper_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.getenv("PGHOST"), "requires PostgreSQL; everything runs in one rolled-back transaction")
class AgentPaperLedgerIntegrationTests(unittest.TestCase):
    def test_initialize_decide_match_and_report_in_a_rolled_back_transaction(self):
        import psycopg
        from psycopg.rows import dict_row

        from app.agent_paper import repository as repo
        from app.agent_paper.report import status
        from app.agent_paper.runner import Runner

        connection = psycopg.connect(row_factory=dict_row, connect_timeout=10)
        try:
            connection.execute("SET statement_timeout='60s'")

            class Op:
                def execute(self, sql):
                    connection.execute(sql)

            migration = _load_migration()
            migration.op = Op()
            migration.upgrade()
            day = date(2026, 9, 17)
            connection.execute("""INSERT INTO quant.broker_portfolio_snapshots(account_key,source,source_snapshot_key,observed_at,verification,
                                   cash,total_asset,total_market_value,content_hash)
                                  VALUES('agent-test-human','test','agent-test',%s,'verified_exact',10,10710,10700,repeat('a',64))
                                  RETURNING snapshot_id""", (datetime(2026, 9, 17, 11, 42, tzinfo=SH),))
            snapshot_id = connection.execute("SELECT snapshot_id FROM quant.broker_portfolio_snapshots WHERE source_snapshot_key='agent-test'").fetchone()["snapshot_id"]
            connection.execute("""INSERT INTO quant.broker_position_snapshots(snapshot_id,symbol,name,quantity,sellable_quantity,average_cost,market_price,market_value)
                                  VALUES(%s,'600664.SH','哈药股份',1000,600,7.0,7.1,7100)""", (snapshot_id,))

            class Database:
                @contextmanager
                def transaction(self):
                    with connection.transaction():
                        yield connection

            quotes = {"600664.SH": book(price="7.30", pre_close="7.20", bids=(("7.29", 50),), asks=(("7.30", 20),)),
                      "002185.SZ": {**book(price="16.50", pre_close="16.40", bids=(("16.49", 50),), asks=(("16.50", 3),)),
                                    "ts_code": "002185.SZ", "name": "华天科技"}}

            async def fetch(symbols):
                return {s: quotes[s] for s in symbols if s in quotes}

            async def fake_context(factory, **kwargs):
                with factory() as conn:
                    conn.execute("SELECT 1")
                return {"account": kwargs["account"]["account_key"]}, {}

            class Model:
                model = "fake"

                def decide(self, context_json):
                    return ModelResult(output={"market_view": "test", "focus_symbols": ["002185.SZ"], "notes": "n", "orders": [
                        {"action": "buy", "symbol": "002185.SZ", "quantity": 500, "order_type": "market", "reason": "depth test"},
                        {"action": "sell", "symbol": "600664.SH", "quantity": 600, "order_type": "limit", "limit_price": 7.5, "reason": "rest"},
                        {"action": "buy", "symbol": "600664.SH", "quantity": 150, "reason": "bad lot"},
                    ]}, model="fake", duration_ms=1, transcript=[{"type": "tool_use", "name": "WebSearch", "input": {"query": "q"}}])

            from app.agent_paper.runner import initialize_account
            from app.agent_paper import runner as runner_module
            connection.execute("""INSERT INTO quant.canonical_bars_daily(symbol,trading_date,open,high,low,close,selected_provider,quality_status,available_at,canonicalized_at)
                                  VALUES('600664.SH','2026-09-16',7,7.3,6.9,7.2,'test','partial',now(),now())
                                  ON CONFLICT DO NOTHING""")
            baseline = initialize_account(Database(), account_key="agent-test", model="fake", source_account="agent-test-human",
                                          start_at=datetime(2026, 9, 17, 13, 0, tzinfo=SH))
            self.assertEqual(baseline["cash"], Decimal("10.00"))
            # A same-day snapshot keeps its T+1 split and is marked at its own prices.
            self.assertEqual(baseline["positions"][0]["sellable_quantity"], 600)
            self.assertEqual(baseline["initial_equity"], Decimal("7110.00"))
            now = datetime(2026, 9, 17, 13, 5, tzinfo=SH)
            runner = Runner(Database(), "agent-test", Model(), fetch_quotes=fetch, context_builder=fake_context, clock=lambda: now)
            # The starting book is already sellable on the start date, so no roll is due.
            self.assertFalse(runner.start_of_day(now))
            outcome = asyncio.run(runner.decide(now))
            by_symbol = {(o.get("symbol"), o.get("side")): o for o in outcome["orders"]}
            # Only 300 shares of 002185 are visible and cash is 10 yuan: the buy cannot fill.
            self.assertEqual(by_symbol[("002185.SZ", "buy")]["status"], "rejected")
            self.assertEqual(by_symbol[("600664.SH", "sell")]["status"], "open")
            self.assertTrue(any(o["status"] == "rejected" and "not_board_lot" in o.get("reasons", []) for o in outcome["orders"]))
            trail = connection.execute("SELECT context,transcript,outcomes FROM quant.agent_paper_decisions WHERE decision_id=%s",
                                       (outcome["decision_id"],)).fetchone()
            # What the agent saw, what it looked up, and every proposed order, including the one rejected before the ledger.
            self.assertEqual(trail["context"], {"account": "agent-test"})
            self.assertEqual(trail["transcript"][0]["input"], {"query": "q"})
            self.assertEqual(len(trail["outcomes"]), 3)
            connection.execute("""INSERT INTO quant.intraday_quote_observations(symbol,source_name,observed_at,price,raw)
                                  VALUES('600664.SH','agent-test',%s,7.52,'{}'::jsonb)""", (now + timedelta(minutes=1),))
            later = now + timedelta(minutes=2)
            matched = asyncio.run(runner.match_open_orders(later))
            self.assertEqual(matched[0]["filled"], 600)
            self.assertEqual(matched[0]["price"], 7.5)
            account = repo.load_account(connection, "agent-test")
            self.assertEqual(account["cash"], Decimal("10") + Decimal("4500") - Decimal("9.50"))
            self.assertEqual(account["memory"]["focus_symbols"], ["002185.SZ"])
            asyncio.run(runner.snapshot_nav(later))
            report = status(connection, account_key="agent-test", day=day)
            self.assertEqual(report["daily"][0]["agent_equity"], float(account["cash"] + Decimal("7.30") * 400))
            self.assertEqual(report["decisions_today"], {"decided": 1, "failed": 0})
            self.assertTrue(runner_module)
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
