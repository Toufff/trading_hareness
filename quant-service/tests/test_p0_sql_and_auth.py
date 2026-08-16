"""Regression coverage for P0 data guards that mocks cannot prove.

The SQL case intentionally runs only where a PostgreSQL service is configured
(the compose test container).  It writes one reserved future-date test row and
removes every row it created in ``finally``.
"""

from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from psycopg.types.json import Json

from app.analyst_expert_research import rebuild_analyst_opinions
from app.episode_lifecycle import backfill_signal_event_episode_links, ensure_signal_episode
from app.analyst_observations import persist_extraction_run, persist_observations_for_evidence
from app.feature_snapshot_repository import materialize_feature_snapshot
from app.intraday_event_retention import prune_ephemeral_signal_events
from app.main import DailyBar, app, db, executor_saturated_response, upsert_bar
from app.runtime_executors import ExecutorSaturatedError


class WriteAuthenticationMiddlewareTests(unittest.TestCase):
    def test_daily_bar_repository_has_no_http_or_main_orchestration_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "daily_bar_repository.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("def upsert_daily_bar", source)

    def test_asgi_middleware_rejects_unsigned_writes_and_allows_valid_key(self) -> None:
        # Exercise the mounted production application and an actual protected
        # route.  Startup is intentionally no-op here: the request body is
        # invalid, so a valid key can prove middleware passage via HTTP 422
        # without touching PostgreSQL or starting background market loops.
        @asynccontextmanager
        async def no_lifespan(_: object):
            yield

        original_lifespan = app.router.lifespan_context
        previous = os.environ.get("QUANT_WRITE_API_KEY")
        os.environ["QUANT_WRITE_API_KEY"] = "test-write-key"
        try:
            app.router.lifespan_context = no_lifespan
            with TestClient(app) as client:
                self.assertEqual(client.get("/openapi.json").status_code, 200)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}).status_code, 401)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}, headers={"X-Quant-Write-Key": "wrong"}).status_code, 401)
                self.assertEqual(client.post("/api/v1/market/bars/import", json={}, headers={"X-Quant-Write-Key": "test-write-key"}).status_code, 422)
                # New replay writes are protected by the identical app-wide
                # gate.  An intentionally invalid payload proves passage with
                # the valid key without invoking its DB-backed replay runner.
                self.assertEqual(client.post("/api/v1/strategies/intraday/replay-recorded-inputs", json={"max_rows": 0}).status_code, 401)
                self.assertEqual(client.post("/api/v1/strategies/intraday/replay-recorded-inputs", json={"max_rows": 0}, headers={"X-Quant-Write-Key": "test-write-key"}).status_code, 422)
        finally:
            app.router.lifespan_context = original_lifespan
            if previous is None:
                os.environ.pop("QUANT_WRITE_API_KEY", None)
            else:
                os.environ["QUANT_WRITE_API_KEY"] = previous

    def test_executor_saturation_has_a_retryable_http_response(self) -> None:
        response = __import__("asyncio").run(executor_saturated_response(None, ExecutorSaturatedError("full")))
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"temporarily saturated", response.body)

    def test_feature_snapshot_keeps_raw_close_separate_from_adjusted_research_close(self) -> None:
        """A recommender must never compare raw close with adjusted SMA."""
        class Result:
            def __init__(self, *, rows=None, row=None):
                self._rows, self._row = rows or [], row

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._row

        class Connection:
            def __init__(self):
                self.writes = []
                ascending = [
                    {
                        "trading_date": date(2026, 1, index + 1), "close": 10 + index,
                        "high": 10 + index, "low": 10 + index, "volume": 100,
                        "amount": 1000, "adj_factor": 2.0, "is_suspended": False,
                        "limit_up": None, "limit_down": None, "selected_provider": "super_sdk",
                    }
                    for index in range(21)
                ]
                self.bars_descending = list(reversed(ascending))

            def execute(self, sql, _params):
                if "FROM quant.universe_members" in sql:
                    return Result(rows=[{"symbol": "000001.SZ", "name": "Test", "industry": "Test", "is_st": False}])
                if "FROM quant.canonical_bars_daily" in sql:
                    return Result(rows=self.bars_descending)
                if "FROM quant.daily_fundamentals" in sql:
                    return Result(row=None)
                if "INSERT INTO quant.feature_snapshots" in sql:
                    self.writes.append(sql)
                    return Result()
                raise AssertionError(f"unexpected SQL: {sql}")

        connection = Connection()
        result = materialize_feature_snapshot(
            connection, date(2026, 1, 21), "core", feature_version="p0-test",
            number=float, market_regime=lambda *_: "neutral",
            analyst_text_factor_summary=lambda *_: {"market": {}},
            latest_tushare_row=lambda *_: None, analyst_feature=lambda *_: {},
        )
        feature = result["items"][0]["features"]
        self.assertEqual(feature["close"], 30.0)
        self.assertEqual(feature["research_close"], 60.0)
        self.assertEqual(feature["sma_20"], 41.0)
        self.assertNotIn("adj_factor_missing", result["items"][0]["quality_flags"])
        self.assertEqual(len(connection.writes), 1)


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class UpsertBarSqlIntegrationTests(unittest.TestCase):
    symbol = "999999.SZ"
    trading_date = date(2099, 1, 2)
    source = "p0-sql-regression"

    def _cleanup(self) -> None:
        # Delete in dependency order.  The reserved symbol/date/source make
        # this safe even when this test is interrupted and rerun.
        with db.transaction() as connection:
            connection.execute(
                "DELETE FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
                (self.symbol, self.trading_date),
            )
            connection.execute(
                "DELETE FROM quant.market_bars_daily WHERE symbol=%s AND trading_date=%s",
                (self.symbol, self.trading_date),
            )
            connection.execute(
                "DELETE FROM quant.raw_market_observations WHERE provider_key=%s AND symbol=%s",
                (self.source, self.symbol),
            )
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def test_null_control_fields_do_not_erase_existing_sql_values(self) -> None:
        self._cleanup()
        observed = datetime(2099, 1, 2, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                upsert_bar(connection, DailyBar(
                    symbol=self.symbol, trading_date=self.trading_date,
                    open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"),
                    adj_factor=Decimal("1.25"), is_suspended=True, is_st=True,
                    source=self.source, available_at=observed,
                ))
                # A normal OHLC feed has no control-plane flags or adjustment
                # factor.  SQL CASE/coalesce must preserve known values.
                upsert_bar(connection, DailyBar(
                    symbol=self.symbol, trading_date=self.trading_date,
                    open=Decimal("10"), high=Decimal("10"), low=Decimal("10"), close=Decimal("10"),
                    adj_factor=None, is_suspended=None, is_st=None,
                    source=self.source, available_at=observed,
                ))
                instrument = connection.execute(
                    "SELECT is_st FROM quant.instruments WHERE symbol=%s", (self.symbol,)
                ).fetchone()
                market = connection.execute(
                    "SELECT adj_factor,is_suspended FROM quant.market_bars_daily WHERE symbol=%s AND trading_date=%s",
                    (self.symbol, self.trading_date),
                ).fetchone()
                canonical = connection.execute(
                    "SELECT adj_factor,is_suspended FROM quant.canonical_bars_daily WHERE symbol=%s AND trading_date=%s",
                    (self.symbol, self.trading_date),
                ).fetchone()
            self.assertTrue(instrument["is_st"])
            self.assertEqual(Decimal(market["adj_factor"]), Decimal("1.25"))
            self.assertTrue(market["is_suspended"])
            self.assertEqual(Decimal(canonical["adj_factor"]), Decimal("1.25"))
            self.assertTrue(canonical["is_suspended"])
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class EphemeralIntradayEventRetentionSqlIntegrationTests(unittest.TestCase):
    """Prove cleanup cannot remove user-facing or settlement-backed events."""

    symbol = "999996.SZ"
    # A historical reserved timestamp keeps the test cutoff strictly before
    # any production event. Never use a far-future cutoff here: the retention
    # SQL intentionally has no symbol filter in production.
    observed_at = datetime(2000, 1, 2, 1, 0, tzinfo=timezone.utc)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.intraday_signal_events WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))

    def _event(self, connection, state: str) -> str:
        row = connection.execute(
            """INSERT INTO quant.intraday_signal_events(
                   symbol,signal_key,signal_type,severity,state,score,observed_at,conditions,evidence,risk_flags
               ) VALUES(%s,%s,'watch','info',%s,1,%s,%s,%s,%s)
               RETURNING signal_event_id""",
            (self.symbol, f"{self.symbol}:watch:{state}", state, self.observed_at, Json({}), Json({}), Json([])),
        ).fetchone()
        return str(row["signal_event_id"])

    def test_only_unreferenced_ephemeral_event_is_deleted(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.instruments(symbol,exchange,name) VALUES(%s,'SZ','Retention')", (self.symbol,))
                removable = self._event(connection, "suppressed")
                delivered = self._event(connection, "confirming")
                outcome_backed = self._event(connection, "suppressed")
                preserved = self._event(connection, "confirmed")
                connection.execute(
                    """INSERT INTO quant.intraday_alert_deliveries(signal_event_id,channel,status)
                       VALUES(%s,'retention-test','suppressed')""",
                    (delivered,),
                )
                connection.execute(
                    """INSERT INTO quant.intraday_signal_outcomes(
                           signal_event_id,horizon_key,direction,entry_observed_at,entry_price,status,tradability,source_status
                       ) VALUES(%s,'5m',1,%s,10,'unavailable','observed_quote_only','{}'::jsonb)""",
                    (outcome_backed, self.observed_at),
                )
                result = prune_ephemeral_signal_events(
                    connection, cutoff=datetime(2001, 1, 1, tzinfo=timezone.utc),
                )
                remaining = {
                    str(row["signal_event_id"])
                    for row in connection.execute(
                        "SELECT signal_event_id FROM quant.intraday_signal_events WHERE symbol=%s", (self.symbol,)
                    ).fetchall()
                }
            self.assertEqual(result["total"], 1)
            self.assertNotIn(removable, remaining)
            self.assertIn(delivered, remaining)
            self.assertIn(outcome_backed, remaining)
            self.assertIn(preserved, remaining)
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class AnalystOpinionMaterializationSqlIntegrationTests(unittest.TestCase):
    """Ensure one corrected source only invalidates its own derived outcome."""

    analyst_id = "p2-opinion-materialization-test"
    report_id = "p2-opinion-materialization-report"
    subject = "999998.SZ"
    as_of_date = date(2099, 1, 2)

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute(
                """DELETE FROM quant.analyst_opinion_outcomes o USING quant.analyst_opinions p
                     WHERE o.opinion_id=p.opinion_id AND p.remote_analyst_id=%s""",
                (self.analyst_id,),
            )
            connection.execute("DELETE FROM quant.analyst_opinions WHERE remote_analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.analyst_claims WHERE remote_analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_reports WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_analysts WHERE remote_analyst_id=%s", (self.analyst_id,))

    def test_source_change_keeps_identity_and_invalidates_only_its_outcome(self) -> None:
        self._cleanup()
        available_at = datetime(2099, 1, 2, 1, 0, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                connection.execute(
                    """INSERT INTO quant.remote_analysts(remote_analyst_id,name)
                       VALUES(%s,%s)""", (self.analyst_id, "P2 materialization test"),
                )
                connection.execute(
                    """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,remote_version,content_hash)
                       VALUES(%s,%s,%s,%s,%s,%s)""",
                    (self.report_id, self.analyst_id, self.as_of_date, "test", "v1", "a" * 64),
                )
                evidence = connection.execute(
                    """INSERT INTO quant.analyst_evidence(remote_report_id,evidence_key,evidence_type,body,content_sha256,available_at)
                       VALUES(%s,%s,%s,%s,%s,%s) RETURNING evidence_id""",
                    (self.report_id, "claim-1", "paragraph", "test claim", "b" * 64, available_at),
                ).fetchone()
                claim = connection.execute(
                    """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,
                              horizon_days,extraction_confidence,explicitness,extractor_version,available_at,published_at)
                       VALUES(%s,%s,'stock',%s,%s,1,0.80,5,0.90,1.0,'p2-test',%s,%s) RETURNING claim_id""",
                    (evidence["evidence_id"], self.analyst_id, self.subject, "P2 test stock", available_at, available_at),
                ).fetchone()

                rebuild_analyst_opinions(connection, self.as_of_date, self.analyst_id)
                first = connection.execute(
                    "SELECT opinion_id FROM quant.analyst_opinions WHERE remote_analyst_id=%s", (self.analyst_id,)
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.analyst_opinion_outcomes(opinion_id,horizon_days,status,methodology_version)
                       VALUES(%s,5,'pending','p2-sql-test')""", (first["opinion_id"],),
                )
                connection.execute("UPDATE quant.analyst_claims SET strength=0.30 WHERE claim_id=%s", (claim["claim_id"],))

                result = rebuild_analyst_opinions(connection, self.as_of_date, self.analyst_id)
                second = connection.execute(
                    "SELECT opinion_id FROM quant.analyst_opinions WHERE remote_analyst_id=%s", (self.analyst_id,)
                ).fetchone()
                remaining = connection.execute(
                    "SELECT count(*)::int count FROM quant.analyst_opinion_outcomes WHERE opinion_id=%s", (second["opinion_id"],)
                ).fetchone()["count"]

            self.assertEqual(first["opinion_id"], second["opinion_id"])
            self.assertEqual(result["invalidated_outcomes"], 1)
            self.assertEqual(remaining, 0)
        finally:
            self._cleanup()


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class Phase1LedgerSqlIntegrationTests(unittest.TestCase):
    """Verify append-only analyst facts and episode continuity in PostgreSQL."""

    analyst_id = "phase1-ledger-test"
    report_id = "phase1-ledger-report"
    symbol = "999997.SZ"

    def _cleanup(self) -> None:
        with db.transaction() as connection:
            connection.execute("DELETE FROM quant.analyst_observations WHERE analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.analyst_extraction_runs WHERE analyst_id=%s", (self.analyst_id,))
            connection.execute("DELETE FROM quant.intraday_signal_episodes WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.instruments WHERE symbol=%s", (self.symbol,))
            connection.execute("DELETE FROM quant.remote_reports WHERE remote_report_id=%s", (self.report_id,))
            connection.execute("DELETE FROM quant.remote_analysts WHERE remote_analyst_id=%s", (self.analyst_id,))

    def test_episode_reuses_same_pulse_then_rearms_after_gap(self) -> None:
        self._cleanup()
        first_at = datetime(2099, 1, 2, 1, 0, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.instruments(symbol,exchange,name) VALUES(%s,'SZ','Phase1')", (self.symbol,))
                signal = {"symbol": self.symbol, "signal_key": f"{self.symbol}:watch:green_reclaim_research_v1",
                          "signal_type": "watch", "conditions": {"setup": "green_reclaim_price_volume_vwap", "price": 10.0, "volume_ratio": 2.0}}
                one = ensure_signal_episode(connection, signal, first_at, "confirmed")
                two = ensure_signal_episode(connection, {**signal, "conditions": {**signal["conditions"], "price": 10.001}},
                                             first_at + __import__("datetime").timedelta(minutes=1), "suppressed")
                three = ensure_signal_episode(connection, signal, first_at + __import__("datetime").timedelta(minutes=7), "confirmed")
                count = connection.execute("SELECT count(*)::int count FROM quant.intraday_signal_episodes WHERE symbol=%s", (self.symbol,)).fetchone()["count"]
            self.assertEqual(one["episode_id"], two["episode_id"])
            self.assertNotEqual(one["episode_id"], three["episode_id"])
            self.assertEqual(count, 2)
        finally:
            self._cleanup()

    def test_episode_accepts_live_rule_payload_with_explicit_symbol(self) -> None:
        """Live signal rule payloads need not repeat the enclosing watch symbol."""
        self._cleanup()
        first_at = datetime(2099, 1, 2, 1, 0, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.instruments(symbol,exchange,name) VALUES(%s,'SZ','Phase1')", (self.symbol,))
                signal = {"signal_key": f"{self.symbol}:watch:volume_anomaly", "signal_type": "watch",
                          "conditions": {"price": 10.0, "volume_ratio": 2.0}}
                episode = ensure_signal_episode(connection, signal, first_at, "confirming", symbol=self.symbol)
            self.assertTrue(episode["episode_id"])
        finally:
            self._cleanup()

    def test_backfill_links_legacy_event_without_external_io(self) -> None:
        self._cleanup()
        first_at = datetime(2099, 1, 2, 1, 0, tzinfo=timezone.utc)
        event_id = None
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.instruments(symbol,exchange,name) VALUES(%s,'SZ','Phase1')", (self.symbol,))
                event_id = connection.execute(
                    """INSERT INTO quant.intraday_signal_events(symbol,signal_key,signal_type,severity,state,score,observed_at,conditions,evidence,risk_flags)
                       VALUES(%s,%s,'watch','info','confirming',1,%s,%s,%s,%s) RETURNING signal_event_id""",
                    (self.symbol, f"{self.symbol}:watch:volume_anomaly", first_at,
                     Json({"price": 10.0, "volume_ratio": 2.0}), Json({}), Json([])),
                ).fetchone()["signal_event_id"]
                result = backfill_signal_event_episode_links(connection, limit=10, symbols=[self.symbol])
                linked = connection.execute(
                    "SELECT episode_id FROM quant.intraday_signal_events WHERE signal_event_id=%s", (event_id,)
                ).fetchone()["episode_id"]
            self.assertEqual(result["linked"], 1)
            self.assertIsNotNone(linked)
        finally:
            self._cleanup()

    def test_observation_is_append_only_for_same_source_key(self) -> None:
        self._cleanup()
        available_at = datetime(2099, 1, 2, 1, 0, tzinfo=timezone.utc)
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.remote_analysts(remote_analyst_id,name) VALUES(%s,'Phase1')", (self.analyst_id,))
                connection.execute(
                    """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,remote_version,content_hash)
                       VALUES(%s,%s,%s,'Phase1','v1,%s')""".replace("'v1,%s'", "'v1',%s"),
                    (self.report_id, self.analyst_id, date(2099, 1, 2), "e" * 64),
                )
                evidence = connection.execute(
                    """INSERT INTO quant.analyst_evidence(remote_report_id,evidence_key,evidence_type,body,content_sha256,available_at)
                       VALUES(%s,'p1','paragraph','看好','c'::text,%s) RETURNING evidence_id""", (self.report_id, available_at),
                ).fetchone()
                connection.execute(
                    """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,horizon_days,
                         extraction_confidence,explicitness,extractor_version,available_at)
                       VALUES(%s,%s,'stock',%s,'Phase1',1,0.8,5,0.9,1.0,'p1',%s)""",
                    (evidence["evidence_id"], self.analyst_id, self.symbol, available_at),
                )
                run = persist_extraction_run(connection, analyst_id=self.analyst_id, source_kind="report", source_id=self.report_id,
                                              source_version="v1", content_hash="d" * 64)
                first = persist_observations_for_evidence(connection, evidence_id=evidence["evidence_id"], extraction_run_id=run,
                    analyst_id=self.analyst_id, source_kind="report", source_id=self.report_id, source_version="v1", content_hash="d" * 64,
                    received_at=available_at, strategy_available_at=available_at, published_at=None, edited_at=None, stated_at=None, stated_precision=None)
                second = persist_observations_for_evidence(connection, evidence_id=evidence["evidence_id"], extraction_run_id=run,
                    analyst_id=self.analyst_id, source_kind="report", source_id=self.report_id, source_version="v1", content_hash="d" * 64,
                    received_at=available_at, strategy_available_at=available_at, published_at=None, edited_at=None, stated_at=None, stated_precision=None)
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
        finally:
            with db.transaction() as connection:
                connection.execute("DELETE FROM quant.analyst_claims WHERE remote_analyst_id=%s", (self.analyst_id,))
                connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (self.report_id,))
            self._cleanup()

    def test_extraction_runs_are_immutable_attempts(self) -> None:
        self._cleanup()
        try:
            with db.transaction() as connection:
                connection.execute("INSERT INTO quant.remote_analysts(remote_analyst_id,name) VALUES(%s,'Phase1')", (self.analyst_id,))
                first = persist_extraction_run(connection, analyst_id=self.analyst_id, source_kind="message",
                                                source_id="msg-run-1", source_version="v1", content_hash="h" * 64,
                                                candidate_count=1, accepted_count=0)
                second = persist_extraction_run(connection, analyst_id=self.analyst_id, source_kind="message",
                                                 source_id="msg-run-1", source_version="v1", content_hash="h" * 64,
                                                 candidate_count=2, accepted_count=1)
                rows = connection.execute(
                    "SELECT candidate_count,accepted_count FROM quant.analyst_extraction_runs "
                    "WHERE source_id='msg-run-1' ORDER BY candidate_count"
                ).fetchall()
            self.assertNotEqual(first, second)
            self.assertEqual([(row["candidate_count"], row["accepted_count"]) for row in rows], [(1, 0), (2, 1)])
        finally:
            self._cleanup()
