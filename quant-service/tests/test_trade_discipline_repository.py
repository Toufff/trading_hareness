"""Append-only persistence for discipline plans, against an in-memory connection.

The double parses the real INSERT column lists and binds them to the real
parameters, so a column/parameter mismatch fails here instead of in production.
No live database is required.
"""

from __future__ import annotations

import re
import unittest
import uuid
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from app.trade_discipline.contracts import (
    Action,
    ComplianceRecord,
    Derivation,
    DisciplinePlan,
    Evaluation,
    Line,
    LineState,
    QualityCheck,
    Review,
    Sizing,
)
from app.trade_discipline.repository import (
    DisciplineFactConflict,
    content_hash,
    latest_evaluation,
    latest_plans,
    mark_superseded,
    persist_compliance,
    persist_evaluation,
    persist_generation_run,
    persist_plan,
    persist_review,
    plan_compliance,
    plan_reviews,
    read_generation_run,
    read_plan,
    read_plan_by_key,
)

SH = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 18, 15, 30, tzinfo=SH)
RUN_ID = "55555555-5555-5555-5555-555555555555"
PRIMARY_KEYS = {"discipline_generation_runs": "run_id", "discipline_plans": "plan_id",
                "discipline_evaluations": "evaluation_id", "discipline_compliance": "compliance_id",
                "discipline_reviews": "review_id"}


def hard_stop_line(price: str = "7.75") -> Line:
    return Line(kind="hard_stop", label=f"日线收盘跌破{price}清仓", metric="daily_close", op="<",
                price=Decimal(price), action=Action(type="exit_all"),
                derivation=Derivation(rule_id="hard_stop.crash_rebound", inputs={"band_low": float(price)},
                                      formula="band_low"),
                priority=1)


def plan(*, plan_key: str = "citics-primary:600613.SH:2026-09-18:holding", status: str = "active",
         hard_stop: str = "7.75", quality: list[QualityCheck] | None = None,
         symbol: str = "600613.SH", as_of: datetime = AS_OF) -> DisciplinePlan:
    return DisciplinePlan(
        plan_key=plan_key, run_id=RUN_ID, account_key="citics-primary", symbol=symbol, name="神奇制药",
        plan_kind="holding", stage="crash_rebound", template_key="crash_rebound@v1", template_version="v1",
        as_of_at=as_of, trading_date=as_of.date(), valid_until=datetime(2026, 9, 25, 15, 0, tzinfo=SH),
        metrics={"close": 8.41, "atr14": 0.73},
        sizing=Sizing(equity=Decimal("99632"), risk_per_trade_pct=Decimal("1.0"), reference_price=Decimal("8.41"),
                      hard_stop=Decimal(hard_stop), stop_distance=Decimal("0.66"), risk_amount=Decimal("996.32"),
                      max_shares=1500, target_exposure_pct=Decimal("20"), current_shares=5800,
                      current_exposure_pct=Decimal("48.96"), recommended_shares=1500),
        lines=[hard_stop_line(hard_stop)], evidence_refs=[f"run:{RUN_ID}"],
        quality=quality if quality is not None else [QualityCheck(check_id="has_hard_stop", passed=True)],
        status=status, inputs_hash="b" * 64, generator_version="trade-discipline-generator-v1")


class Result:
    def __init__(self, rows):
        self.rows = list(rows)
        self.rowcount = len(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    """A tiny append-only store that speaks the repository's actual SQL."""

    def __init__(self):
        self.tables: dict[str, list[dict]] = {name: [] for name in PRIMARY_KEYS}
        self.instruments: list[tuple] = []
        self.statements: list[str] = []

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _plain(value):
        return value.obj if isinstance(value, Json) else value

    def _insert(self, sql: str, params: tuple) -> Result:
        table = re.search(r"INSERT INTO quant\.(\w+)", sql).group(1)
        columns = [name.strip() for name in re.search(r"INSERT INTO quant\.\w+\(([^)]*)\)", sql).group(1).split(",")]
        assert len(columns) == len(params), f"{table}: {len(columns)} columns but {len(params)} parameters"
        row = {column: self._plain(value) for column, value in zip(columns, params)}
        key = PRIMARY_KEYS[table]
        row.setdefault(key, str(uuid.uuid4()))
        row["created_at"] = datetime(2026, 9, 18, 16, 0, tzinfo=SH)
        self.tables[table].append(row)
        returning = re.search(r"RETURNING ([\w,]+)", sql)
        return Result([{name: row.get(name) for name in returning.group(1).split(",")}] if returning else [])

    # -- connection surface ----------------------------------------------
    def execute(self, sql: str, params=()):  # noqa: C901 - one branch per statement the repository issues
        flat = " ".join(sql.split())
        self.statements.append(flat)
        params = tuple(params)
        if flat.startswith("INSERT INTO quant.instruments"):
            self.instruments.append(params)
            return Result([])
        if flat.startswith("INSERT INTO"):
            return self._insert(flat, params)
        if flat.startswith("UPDATE quant.discipline_plans SET status='superseded'"):
            plan_id, by_plan_id = params
            hits = [row for row in self.tables["discipline_plans"]
                    if str(row["plan_id"]) == plan_id and row["status"] == "active" and str(row["plan_id"]) != by_plan_id]
            for row in hits:
                row["status"] = "superseded"
            return Result(hits)
        if "FROM quant.discipline_generation_runs" in flat:
            return Result([row for row in self.tables["discipline_generation_runs"] if str(row["run_id"]) == params[0]])
        if "FROM quant.discipline_plans" in flat:
            rows = self.tables["discipline_plans"]
            if "WHERE plan_key=" in flat:
                return Result([row for row in rows if row["plan_key"] == params[0]])
            if "WHERE plan_id=" in flat:
                return Result([row for row in rows if str(row["plan_id"]) == params[0]])
            account_key, statuses, limit = params
            newest: dict[str, dict] = {}
            for row in sorted(rows, key=lambda item: item["as_of_at"]):
                if row["account_key"] == account_key and row["status"] in statuses:
                    newest[row["symbol"]] = row
            return Result(sorted(newest.values(), key=lambda item: (item["as_of_at"], item["symbol"]),
                                 reverse=True)[:limit])
        if "FROM quant.discipline_evaluations" in flat:
            rows = self.tables["discipline_evaluations"]
            if "WHERE evaluation_id=" in flat:
                return Result([row for row in rows if str(row["evaluation_id"]) == params[0]])
            if "AND basis=%s" in flat:
                return Result([row for row in rows if str(row["plan_id"]) == params[0]
                               and row["as_of_at"] == params[1] and row["basis"] == params[2]])
            plan_id, basis, _ = params
            found = [row for row in rows if str(row["plan_id"]) == plan_id and (basis is None or row["basis"] == basis)]
            return Result(sorted(found, key=lambda item: item["as_of_at"], reverse=True)[:1])
        if "FROM quant.discipline_compliance" in flat:
            rows = self.tables["discipline_compliance"]
            if "WHERE compliance_id=" in flat:
                return Result([row for row in rows if str(row["compliance_id"]) == params[0]])
            if "AND content_hash=" in flat:
                return Result([row for row in rows if str(row["plan_id"]) == params[0]
                               and row["content_hash"] == params[1]])
            return Result([row for row in rows if str(row["plan_id"]) == params[0]][:params[1]])
        if "FROM quant.discipline_reviews" in flat:
            rows = self.tables["discipline_reviews"]
            if "WHERE review_id=" in flat:
                return Result([row for row in rows if str(row["review_id"]) == params[0]])
            if "AND content_hash=" in flat:
                return Result([row for row in rows if str(row["plan_id"]) == params[0]
                               and row["content_hash"] == params[1]])
            return Result([row for row in rows if str(row["plan_id"]) == params[0]][:params[1]])
        raise AssertionError(f"unexpected statement: {flat}")


class PlanPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()

    def test_a_plan_is_inserted_registered_and_read_back(self):
        result = persist_plan(self.connection, plan())
        self.assertEqual(result["status"], "created")
        self.assertRegex(result["content_hash"], r"^[0-9a-f]{64}$")
        stored = result["plan"]
        self.assertEqual(stored["plan_key"], "citics-primary:600613.SH:2026-09-18:holding")
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["sizing"]["hard_stop"], "7.75")
        self.assertEqual(stored["lines"][0]["kind"], "hard_stop")
        self.assertEqual(self.instrument_symbols(), ["600613.SH"])
        self.assertEqual(read_plan(self.connection, result["plan_id"])["plan_id"], result["plan_id"])
        self.assertEqual(read_plan_by_key(self.connection, stored["plan_key"])["plan_id"], result["plan_id"])

    def instrument_symbols(self):
        return [row[0] for row in self.connection.instruments]

    def test_the_same_plan_twice_is_idempotent_and_appends_nothing(self):
        first = persist_plan(self.connection, plan())
        second = persist_plan(self.connection, plan())
        self.assertEqual(second["status"], "idempotent")
        self.assertEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(second["content_hash"], first["content_hash"])
        self.assertEqual(len(self.connection.tables["discipline_plans"]), 1)

    def test_different_content_under_the_same_key_is_a_conflict_not_an_overwrite(self):
        persist_plan(self.connection, plan())
        with self.assertRaises(DisciplineFactConflict):
            persist_plan(self.connection, plan(hard_stop="7.60"))
        self.assertEqual(len(self.connection.tables["discipline_plans"]), 1)
        self.assertEqual(self.connection.tables["discipline_plans"][0]["sizing"]["hard_stop"], "7.75")

    def test_a_plan_that_failed_the_quality_gate_is_stored_with_its_failures(self):
        failing = plan(status="rejected_by_quality",
                       quality=[QualityCheck(check_id="hard_stop_distance_sane", passed=False,
                                             detail="0.29 < 0.8xATR14")])
        stored = persist_plan(self.connection, failing)["plan"]
        self.assertEqual(stored["status"], "rejected_by_quality")
        self.assertEqual(stored["quality"][0]["passed"], False)
        self.assertEqual(stored["quality"][0]["detail"], "0.29 < 0.8xATR14")

    def test_latest_plans_keeps_one_row_per_symbol_for_this_account(self):
        persist_plan(self.connection, plan())
        persist_plan(self.connection, plan(plan_key="citics-primary:600613.SH:2026-09-17:holding",
                                           as_of=datetime(2026, 9, 17, 15, 30, tzinfo=SH), hard_stop="7.40"))
        persist_plan(self.connection, plan(plan_key="citics-primary:600664.SH:2026-09-18:holding",
                                           symbol="600664.SH"))
        rows = latest_plans(self.connection, "citics-primary")
        self.assertEqual({row["symbol"] for row in rows}, {"600613.SH", "600664.SH"})
        newest = next(row for row in rows if row["symbol"] == "600613.SH")
        self.assertEqual(newest["trading_date"], date(2026, 9, 18))
        self.assertEqual(latest_plans(self.connection, "other-account"), [])

    def test_superseding_only_moves_an_active_plan_and_never_edits_content(self):
        old = persist_plan(self.connection, plan())
        new = persist_plan(self.connection, plan(plan_key="citics-primary:600613.SH:2026-09-19:holding",
                                                 as_of=datetime(2026, 9, 19, 15, 30, tzinfo=SH)))
        self.assertTrue(mark_superseded(self.connection, old["plan_id"], by_plan_id=new["plan_id"]))
        self.assertFalse(mark_superseded(self.connection, old["plan_id"], by_plan_id=new["plan_id"]))
        self.assertFalse(mark_superseded(self.connection, new["plan_id"], by_plan_id=new["plan_id"]))
        self.assertEqual(read_plan(self.connection, old["plan_id"])["status"], "superseded")
        self.assertEqual(read_plan(self.connection, old["plan_id"])["sizing"]["hard_stop"], "7.75")


    def test_two_generations_on_one_trading_day_both_persist_and_the_first_is_superseded(self):
        """Principle 8 must be reachable inside a session, not only across days.

        ``plan_key`` carries the evidence digest, so the 10:00 run and the 14:00
        run of the same day are two storable rows rather than one conflict that
        drops the newer plan on the floor.
        """
        morning = persist_plan(self.connection, plan(
            plan_key="citics-primary:600613.SH:2026-09-18:holding:aaaaaaaaaaaa",
            as_of=datetime(2026, 9, 18, 10, 0, tzinfo=SH)))
        afternoon = persist_plan(self.connection, plan(
            plan_key="citics-primary:600613.SH:2026-09-18:holding:bbbbbbbbbbbb",
            as_of=datetime(2026, 9, 18, 14, 0, tzinfo=SH), hard_stop="7.90"))
        self.assertEqual((morning["status"], afternoon["status"]), ("created", "created"))
        self.assertNotEqual(morning["plan_id"], afternoon["plan_id"])
        self.assertTrue(mark_superseded(self.connection, morning["plan_id"], by_plan_id=afternoon["plan_id"]))
        self.assertEqual(read_plan(self.connection, morning["plan_id"])["status"], "superseded")
        self.assertEqual(read_plan(self.connection, afternoon["plan_id"])["status"], "active")
        self.assertEqual(len(self.connection.tables["discipline_plans"]), 2)

    def test_the_same_day_key_with_identical_content_is_still_idempotent(self):
        key = "citics-primary:600613.SH:2026-09-18:holding:aaaaaaaaaaaa"
        first = persist_plan(self.connection, plan(plan_key=key))
        again = persist_plan(self.connection, plan(plan_key=key))
        self.assertEqual(again["status"], "idempotent")
        self.assertEqual(again["plan_id"], first["plan_id"])
        with self.assertRaises(DisciplineFactConflict):
            persist_plan(self.connection, plan(plan_key=key, hard_stop="7.90"))


class GenerationRunTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.arguments = dict(run_id=RUN_ID, account_key="citics-primary", as_of_at=AS_OF,
                              trading_date=date(2026, 9, 18), generator_version="trade-discipline-generator-v1",
                              inputs_hash="c" * 64, inputs={"symbol": "600613.SH", "bars": 27})

    def test_the_run_stores_the_frozen_inputs_and_is_idempotent(self):
        created = persist_generation_run(self.connection, **self.arguments)
        self.assertEqual(created["status"], "created")
        self.assertEqual(persist_generation_run(self.connection, **self.arguments)["status"], "idempotent")
        self.assertEqual(len(self.connection.tables["discipline_generation_runs"]), 1)
        self.assertEqual(read_generation_run(self.connection, RUN_ID)["inputs"], {"symbol": "600613.SH", "bars": 27})

    def test_a_reused_run_id_with_other_inputs_is_a_conflict(self):
        persist_generation_run(self.connection, **self.arguments)
        with self.assertRaises(DisciplineFactConflict):
            persist_generation_run(self.connection, **{**self.arguments, "inputs_hash": "d" * 64})


class HistoryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = Connection()
        self.plan_id = persist_plan(self.connection, plan())["plan_id"]

    def evaluation(self, *, state: str = "active") -> Evaluation:
        return Evaluation(plan_id=self.plan_id, as_of_at=datetime(2026, 9, 21, 15, 0, tzinfo=SH),
                          trading_date=date(2026, 9, 21), basis="daily",
                          line_states=[LineState(kind="hard_stop", label="日线收盘跌破7.75清仓", state="armed")],
                          plan_state=state, inputs_hash="e" * 64)

    def test_one_evaluation_per_plan_moment_and_basis(self):
        created = persist_evaluation(self.connection, self.evaluation())
        self.assertEqual(created["status"], "created")
        self.assertEqual(created["evaluation"]["plan_state"], "active")
        self.assertEqual(persist_evaluation(self.connection, self.evaluation())["status"], "idempotent")
        self.assertEqual(len(self.connection.tables["discipline_evaluations"]), 1)
        with self.assertRaises(DisciplineFactConflict):
            persist_evaluation(self.connection, self.evaluation(state="exit_signalled"))

    def test_latest_evaluation_returns_the_newest_moment(self):
        persist_evaluation(self.connection, self.evaluation())
        later = self.evaluation().model_copy(update={"as_of_at": datetime(2026, 9, 22, 15, 0, tzinfo=SH),
                                                     "trading_date": date(2026, 9, 22),
                                                     "plan_state": "reduce_signalled"})
        persist_evaluation(self.connection, later)
        self.assertEqual(latest_evaluation(self.connection, self.plan_id)["plan_state"], "reduce_signalled")
        self.assertIsNone(latest_evaluation(self.connection, self.plan_id, basis="minute"))

    def test_compliance_and_reviews_are_append_only_and_content_idempotent(self):
        record = ComplianceRecord(plan_id=self.plan_id, trade_record_id=None, line_kind="hard_stop",
                                  verdict="early", deviation={"price_gap": "0.12", "days": 1},
                                  notes="触发前一日卖出")
        created = persist_compliance(self.connection, record)
        self.assertEqual(created["compliance"]["verdict"], "early")
        self.assertEqual(created["compliance"]["deviation"], {"price_gap": "0.12", "days": 1})
        self.assertEqual(persist_compliance(self.connection, record)["status"], "idempotent")
        self.assertEqual(persist_compliance(self.connection,
                                            record.model_copy(update={"verdict": "late"}))["status"], "created")
        self.assertEqual(len(plan_compliance(self.connection, self.plan_id)), 2)

        review = Review(plan_id=self.plan_id, reviewer="human", verdict="override",
                        notes="硬止损让位给结构低点", overrides={"hard_stop": "8.12"})
        self.assertEqual(persist_review(self.connection, review)["review"]["overrides"], {"hard_stop": "8.12"})
        self.assertEqual(persist_review(self.connection, review)["status"], "idempotent")
        self.assertEqual(len(plan_reviews(self.connection, self.plan_id)), 1)


class HashingTests(unittest.TestCase):
    def test_content_hash_ignores_key_order_but_not_values(self):
        self.assertEqual(content_hash({"a": 1, "b": 2}), content_hash({"b": 2, "a": 1}))
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": "1"}))
        self.assertRegex(content_hash({"a": Decimal("1.5")}), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
