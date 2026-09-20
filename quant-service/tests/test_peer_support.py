"""The peer contract endpoint and the attributed error feed.

The log samples below are copied from the live owner cluster on 2026-09-20
after the ``log_line_prefix`` change, including the real failing statement the
peer deployment repeated 5123 times on 2026-09-19 without noticing.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.peer_contract import NOT_PROVIDED, SUPPORTED_OBJECTS, build_contract
from app.tushare_normalization import (
    SUPERSEDED_MARKER,
    promotable_adjustment_factor,
    promotable_factor_evidence_sql,
)
from app.peer_error_feed import (
    LOG_TIMEZONE,
    collect_role_errors,
    group_entries,
    iter_entries,
    log_files_for_range,
)
from app.routers.peer_support import build_peer_support_router
from app.settings import Settings


PEER_ERROR = (
    '2026-09-20 11:24:44.742 CST [14692] stock_peer@trading_hareness app=[unknown] 42601 '
    'ERROR:  syntax error at or near "$2" at character 122'
)
PEER_STATEMENT = (
    '2026-09-20 11:24:44.742 CST [14692] stock_peer@trading_hareness app=[unknown] 42601 '
    'STATEMENT:  SELECT count(*) FROM quant.intraday_board_flow_snapshots '
    'WHERE source_status ? $1 AND snapshot_minute > now() - interval $2'
)
PEER_ERROR_LATER = (
    '2026-09-20 11:25:01.014 CST [35248] stock_peer@trading_hareness app=[unknown] 42601 '
    'ERROR:  syntax error at or near "$2" at character 122'
)
BACKGROUND_LOG = '2026-09-20 11:19:33.170 CST [101300] LOG:  received SIGHUP, reloading configuration files'
OWNER_ERROR = (
    '2026-09-20 11:30:00.000 CST [22222] quant_app@trading_hareness app=quant-service 40P01 '
    'ERROR:  deadlock detected'
)


class ErrorFeedParsingTests(unittest.TestCase):
    def test_an_error_keeps_the_statement_postgres_logged_beneath_it(self):
        entries = list(iter_entries([PEER_ERROR, PEER_STATEMENT]))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry.role, "stock_peer")
        self.assertEqual(entry.database, "trading_hareness")
        self.assertEqual(entry.sqlstate, "42601")
        self.assertEqual(entry.level, "ERROR")
        self.assertIn("syntax error", entry.message)
        self.assertIn("intraday_board_flow_snapshots", entry.statement)

    def test_unattributed_background_lines_are_never_handed_to_a_consumer(self):
        # %q leaves background workers without a role. Guessing an owner for
        # those lines is exactly the inference this whole feed exists to stop.
        self.assertEqual(list(iter_entries([BACKGROUND_LOG])), [])

    def test_a_continuation_line_extends_the_statement_rather_than_starting_one(self):
        entries = list(iter_entries([PEER_ERROR, PEER_STATEMENT, "\tAND snapshot_minute > now()"]))
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].statement.endswith("AND snapshot_minute > now()"))

    def test_a_statement_from_another_backend_does_not_attach(self):
        other_pid = PEER_STATEMENT.replace("[14692]", "[99999]")
        entries = list(iter_entries([PEER_ERROR, other_pid]))
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].statement)

    def test_repeated_failures_collapse_into_one_counted_row(self):
        entries = list(iter_entries([PEER_ERROR, PEER_STATEMENT, PEER_ERROR_LATER]))
        groups, total = group_entries(entries, limit=10)
        self.assertEqual(total, 3 - 1)  # the STATEMENT line is an attachment, not an entry
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["sqlstate"], "42601")
        self.assertLess(groups[0]["first_seen"], groups[0]["last_seen"])
        # The second occurrence logged no STATEMENT line; keying on the SQL
        # would have split this one repeating failure into two rows of one.
        self.assertIn("intraday_board_flow_snapshots", groups[0]["statement"])

    def test_numbers_inside_a_message_do_not_split_one_problem_into_many(self):
        variant = PEER_ERROR.replace("character 122", "character 133")
        groups, _ = group_entries(iter_entries([PEER_ERROR, variant]), limit=10)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        # The reported text stays a real observed message, not the mask.
        self.assertIn("character 122", groups[0]["message"])


class LogFileSelectionTests(unittest.TestCase):
    def test_only_dated_files_inside_the_window_are_read(self):
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            for name in ("postgresql-2026-09-18.log", "postgresql-2026-09-19.log",
                         "postgresql-2026-09-20.log", "postgresql-startup.log"):
                (directory / name).write_text("", encoding="utf-8")
            since = datetime(2026, 9, 19, 0, 0, tzinfo=LOG_TIMEZONE)
            until = datetime(2026, 9, 20, 23, 59, tzinfo=LOG_TIMEZONE)
            names = [path.name for path in log_files_for_range(directory, since, until)]
            self.assertEqual(names, ["postgresql-2026-09-19.log", "postgresql-2026-09-20.log"])

    def test_a_missing_log_directory_is_not_an_exception(self):
        self.assertEqual(log_files_for_range(Path("does-not-exist"), datetime.now(LOG_TIMEZONE),
                                             datetime.now(LOG_TIMEZONE)), [])


class CollectRoleErrorsTests(unittest.TestCase):
    def _directory(self, stack) -> Path:
        raw = stack.enter_context(TemporaryDirectory())
        directory = Path(raw)
        (directory / "postgresql-2026-09-20.log").write_text(
            "\n".join([PEER_ERROR, PEER_STATEMENT, BACKGROUND_LOG, OWNER_ERROR, PEER_ERROR_LATER]) + "\n",
            encoding="utf-8",
        )
        return directory

    def test_only_the_requested_role_is_reported(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            directory = self._directory(stack)
            payload = collect_role_errors(
                log_dir=directory,
                role="stock_peer",
                since=datetime(2026, 9, 20, 0, 0, tzinfo=LOG_TIMEZONE),
                until=datetime(2026, 9, 20, 23, 59, tzinfo=LOG_TIMEZONE),
            )
        self.assertEqual(payload["role"], "stock_peer")
        self.assertEqual(payload["total_entries"], 2)
        self.assertEqual(payload["distinct_problems"], 1)
        self.assertEqual(payload["groups"][0]["count"], 2)
        # The owner's own deadlock must not leak through the peer's read key.
        self.assertNotIn("deadlock", str(payload["groups"]))

    def test_entries_outside_the_window_are_excluded(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            directory = self._directory(stack)
            payload = collect_role_errors(
                log_dir=directory,
                role="stock_peer",
                since=datetime(2026, 9, 20, 11, 25, tzinfo=LOG_TIMEZONE),
                until=datetime(2026, 9, 20, 23, 59, tzinfo=LOG_TIMEZONE),
            )
        self.assertEqual(payload["total_entries"], 1)

    def test_a_byte_budget_smaller_than_the_file_reports_truncation(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            directory = self._directory(stack)
            payload = collect_role_errors(
                log_dir=directory,
                role="stock_peer",
                since=datetime(2026, 9, 20, 0, 0, tzinfo=LOG_TIMEZONE),
                until=datetime(2026, 9, 20, 23, 59, tzinfo=LOG_TIMEZONE),
                max_bytes=10,
            )
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total_entries"], 0)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    """Answers the catalog reads build_contract makes, in SELECT order."""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        text = " ".join(sql.split())
        if "pg_attribute" in text:
            return _FakeCursor([
                ("daily_adjustment_factors", "symbol", "text", True, None),
                ("daily_adjustment_factors", "adj_factor", "numeric", True, None),
                ("daily_adjustment_factors", "raw", "jsonb", False, None),
            ])
        if "pg_indexes" in text:
            return _FakeCursor([
                ("daily_adjustment_factors", "daily_adjustment_factors_pkey",
                 "CREATE UNIQUE INDEX daily_adjustment_factors_pkey ON quant.daily_adjustment_factors USING btree (symbol, trading_date)"),
            ])
        if "has_table_privilege" in text:
            return _FakeCursor([
                ("daily_adjustment_factors", "pg_default", True, True, True, True),
            ])
        if "pg_tablespace t ON t.oid = c.reltablespace" in text or "WHERE t.spcname" in text:
            return _FakeCursor([("quant.legacy_source_records",), ("quant.raw_market_observations_cold",)])
        if "count(*) FILTER" in text:
            # rows_total, rows_with_key, rows_superseded, first_labelled, last_day.
            # The shape that matters: the key is present on a small minority.
            return _FakeCursor([(4158486, 97187, 61614, date(2026, 8, 21), date(2026, 9, 18))])
        if "DISTINCT provider" in text:
            return _FakeCursor([("longhu_qfq_derived",), ("longhuvip_composite",), ("tushare_primary",)])
        if "factor_semantics" in text:
            return _FakeCursor([("corporate_action_cumulative",), ("same_day_identity_only",)])
        if "alembic_version" in text:
            return _FakeCursor([("20260919_0106",)])
        if "inet_server_port" in text:
            return _FakeCursor([("trading_hareness", 55432, "16.15")])
        raise AssertionError(f"unexpected statement: {text}")


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = build_contract(_FakeConnection())

    def test_the_contract_reports_the_live_migration_head_and_identity(self):
        self.assertEqual(self.contract["alembic_head"], "20260919_0106")
        self.assertEqual(self.contract["database"], "trading_hareness")
        self.assertEqual(self.contract["server_port"], 55432)

    def test_only_the_curated_objects_are_published(self):
        names = [item["name"] for item in self.contract["objects"]]
        self.assertEqual(len(names), len(SUPPORTED_OBJECTS))
        self.assertIn("quant.canonical_bars_daily", names)
        # stock_peer can read 200+ relations by inheritance; publishing them
        # would imply support for whatever happens to be reachable today.
        self.assertNotIn("quant.analyst_opinions", names)

    def test_real_columns_carry_nullability_so_a_check_can_be_written_against_them(self):
        factors = next(item for item in self.contract["objects"] if item["name"].endswith("daily_adjustment_factors"))
        adj = next(column for column in factors["columns"] if column["name"] == "adj_factor")
        self.assertFalse(adj["nullable"])
        self.assertTrue(factors["indexes"])

    def test_every_check_that_broke_the_peer_is_named_as_permanently_absent(self):
        absent = {item["name"] for item in self.contract["not_provided"]}
        for expected in (
            "quant.canonical_bars_daily.adjustment_state",
            "quant.daily_adjustment_factors.factor_semantics",
            "quant.daily_adjustment_factors.retired_at",
            "quant.canonical_bars_daily_cold",
            "adjustment guard indexes",
            "/api/v1/research/storage-tiers",
            "cumulative_tushare",
        ):
            self.assertIn(expected, absent)
        self.assertEqual(len(self.contract["not_provided"]), len(NOT_PROVIDED))

    def test_the_fabricated_factor_semantics_value_is_absent_from_the_live_enumeration(self):
        values = self.contract["enumerations"]["factor_semantics"]["values"]
        self.assertEqual(values, ["corporate_action_cumulative", "same_day_identity_only"])
        self.assertNotIn("cumulative_tushare", values)

    def test_the_enumeration_says_how_rare_the_key_is_not_only_what_it_may_contain(self):
        # v1 published the value set and stopped, which reads as "every row has
        # one of these". 97,187 of 4,158,486 rows do.
        coverage = self.contract["enumerations"]["factor_semantics"]["coverage"]
        self.assertEqual(coverage["rows_total"], 4158486)
        self.assertEqual(coverage["rows_with_key"], 97187)
        self.assertEqual(coverage["first_labelled_trading_date"], "2026-08-21")
        self.assertEqual(coverage["last_trading_date"], "2026-09-18")

    def test_the_document_is_json_serializable_so_dates_never_reach_a_consumer_as_objects(self):
        json.dumps(self.contract)

    def test_a_missing_key_is_named_as_a_documented_state_rather_than_a_fault(self):
        absent = {item["name"]: item["reason"] for item in self.contract["not_provided"]}
        reason = absent["quant.daily_adjustment_factors.raw->>'factor_semantics' NOT NULL"]
        self.assertIn("not an error", reason)
        self.assertIn("derived_rules.adjustment_factor_usable", reason)

    def test_usability_is_published_as_the_owners_own_predicate_not_as_a_description(self):
        rule = self.contract["derived_rules"]["adjustment_factor_usable"]
        self.assertEqual(rule["sql_predicate"], promotable_factor_evidence_sql("factor", "raw"))

    def test_the_predicate_accepts_the_unlabelled_majority_and_refuses_a_superseded_row(self):
        # The three cases a semantics-only reader gets wrong.
        rule = self.contract["derived_rules"]["adjustment_factor_usable"]
        self.assertIn("", rule["promotable_semantics_for_prefix"])
        self.assertEqual(rule["superseded_marker"], SUPERSEDED_MARKER)
        self.assertEqual(rule["rows_superseded"], 61614)
        self.assertTrue(promotable_adjustment_factor({}, provider_key="tushare_primary"))
        self.assertFalse(promotable_adjustment_factor(
            {"factor_semantics": "corporate_action_cumulative", SUPERSEDED_MARKER: "2026-09-07"},
            provider_key="tushare_primary"))
        # A provider the peer can see in the table but may never price from.
        self.assertIn("longhuvip_composite", rule["observed_providers"])
        self.assertFalse(promotable_adjustment_factor(
            {"factor_semantics": "corporate_action_cumulative"}, provider_key="longhuvip_composite"))

    def test_the_rules_forbid_failing_closed_on_an_absent_key(self):
        joined = " ".join(self.contract["rules"])
        self.assertIn("Never fail closed on a missing JSON key", joined)
        self.assertIn("derived_rules[]", joined)

    def test_the_cold_tier_is_published_so_nobody_has_to_query_pg_class_for_it(self):
        self.assertIn("quant.legacy_source_records", self.contract["cold_tier"]["tables"])
        self.assertFalse(self.contract["cold_tier"]["peer_readable"])


class _FakeDatabase:
    def __init__(self):
        self.connection = _FakeConnection()

    def transaction(self):
        connection = self.connection

        class _Scope:
            def __enter__(self):
                return connection

            def __exit__(self, *exc):
                return False

        return _Scope()


class PeerRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = Path(self.tmp.name)
        (self.log_dir / "postgresql-2026-09-20.log").write_text(
            "\n".join([PEER_ERROR, PEER_STATEMENT, OWNER_ERROR, PEER_ERROR_LATER]) + "\n",
            encoding="utf-8",
        )
        app = FastAPI()
        app.include_router(build_peer_support_router(
            _FakeDatabase(),
            shared_read_key=lambda: "peer-key",
            log_directory=lambda: str(self.log_dir),
            allowed_roles=lambda: ("stock_peer",),
            now=lambda: datetime(2026, 9, 20, 12, 0, tzinfo=LOG_TIMEZONE),
        ))
        self.client = TestClient(app)
        self.auth = {"X-Quant-Read-Key": "peer-key"}

    def test_both_endpoints_require_the_shared_read_key(self):
        self.assertEqual(self.client.get("/api/v1/peer/contract").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/peer/errors").status_code, 401)

    def test_the_read_key_does_not_unlock_the_owners_own_errors(self):
        response = self.client.get("/api/v1/peer/errors?role=quant_app", headers=self.auth)
        self.assertEqual(response.status_code, 403)

    def test_the_default_window_returns_the_peers_repeated_failure(self):
        response = self.client.get("/api/v1/peer/errors", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["role"], "stock_peer")
        self.assertEqual(payload["total_entries"], 2)
        self.assertEqual(payload["groups"][0]["sqlstate"], "42601")
        self.assertIn("intraday_board_flow_snapshots", payload["groups"][0]["statement"])
        self.assertEqual(payload["live_effect"], "none")

    def test_an_unparseable_timestamp_is_rejected_rather_than_silently_defaulted(self):
        response = self.client.get("/api/v1/peer/errors?since=yesterday", headers=self.auth)
        self.assertEqual(response.status_code, 422)

    def test_a_window_longer_than_a_month_is_refused(self):
        response = self.client.get(
            "/api/v1/peer/errors?since=2026-01-01T00:00:00&until=2026-09-20T00:00:00", headers=self.auth)
        self.assertEqual(response.status_code, 422)

    def test_an_inverted_window_is_refused(self):
        response = self.client.get(
            "/api/v1/peer/errors?since=2026-09-20T12:00:00&until=2026-09-19T12:00:00", headers=self.auth)
        self.assertEqual(response.status_code, 422)

    def test_a_missing_log_directory_says_so_instead_of_returning_an_empty_feed(self):
        app = FastAPI()
        app.include_router(build_peer_support_router(
            _FakeDatabase(),
            shared_read_key=lambda: "peer-key",
            log_directory=lambda: str(self.log_dir / "absent"),
            allowed_roles=lambda: ("stock_peer",),
        ))
        response = TestClient(app).get("/api/v1/peer/errors", headers=self.auth)
        self.assertEqual(response.status_code, 503)

    def test_the_contract_endpoint_serves_the_introspected_document(self):
        response = self.client.get("/api/v1/peer/contract", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["contract_version"], "peer-contract-v2")
        self.assertEqual(payload["peer_role"], "stock_peer")
        self.assertTrue(payload["rules"])


class PeerSettingsTests(unittest.TestCase):
    def test_the_log_directory_and_role_allowlist_have_production_defaults(self):
        settings = Settings.from_environ({})
        self.assertEqual(settings.postgres_log_dir, "G:/StockPlatform/logs")
        self.assertEqual(settings.peer_consumer_roles, ("stock_peer",))

    def test_both_are_overridable(self):
        settings = Settings.from_environ({
            "QUANT_POSTGRES_LOG_DIR": "/var/log/pg",
            "QUANT_PEER_CONSUMER_ROLES": "stock_peer, other_peer ",
        })
        self.assertEqual(settings.postgres_log_dir, "/var/log/pg")
        self.assertEqual(settings.peer_consumer_roles, ("stock_peer", "other_peer"))


if __name__ == "__main__":
    unittest.main()
