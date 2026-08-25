"""Unit and real-Postgres coverage for the strategy live-promotion gate."""

from __future__ import annotations

import os
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

from app.platform.strategy_registry import STRATEGY_CONTRACTS
from app.strategy_promotion import MAX_APPROVED_WEIGHT, strategy_live_promotion, strategy_promotion_catalog


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def execute(self, _sql, _params):
        return _Result(self._row)


class StrategyPromotionUnitTests(unittest.TestCase):
    def test_missing_registry_row_is_fail_closed(self) -> None:
        result = strategy_live_promotion(_Connection(None), "watchlist_main_wave_shadow", date(2026, 8, 25))
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["weight"], 0.0)
        self.assertEqual(result["reason"], "promotion_registry_missing")

    def test_eligible_for_review_without_approval_stays_zero_weight(self) -> None:
        result = strategy_live_promotion(_Connection({
            "status": "eligible_for_review", "max_live_weight": 0.1, "approved_by": None, "approved_at": None,
            "reason": "awaiting reviewer", "methodology_version": "v1", "evidence": {},
        }), "post_close_base_candidates", date(2026, 8, 25))
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["weight"], 0.0)

    def test_approved_status_is_capped_at_the_ten_percent_ceiling(self) -> None:
        result = strategy_live_promotion(_Connection({
            "status": "approved", "max_live_weight": 0.5, "approved_by": "reviewer",
            "approved_at": datetime.now(timezone.utc), "reason": "approved", "methodology_version": "v1", "evidence": {},
        }), "post_close_base_candidates", date(2026, 8, 25))
        self.assertTrue(result["execution_eligible"])
        self.assertEqual(result["weight"], MAX_APPROVED_WEIGHT)

    def test_catalog_covers_every_declared_strategy_contract(self) -> None:
        rows = strategy_promotion_catalog(_Connection(None), date(2026, 8, 25))
        self.assertEqual({row["strategy_key"] for row in rows}, set(STRATEGY_CONTRACTS))
        self.assertTrue(all(row["execution_eligible"] is False for row in rows))


@unittest.skipUnless(os.getenv("PGHOST"), "requires the compose PostgreSQL service")
class StrategyPromotionRegistrySqlIntegrationTests(unittest.TestCase):
    def test_every_declared_strategy_is_seeded_disabled_and_zero_weight(self) -> None:
        from app.main import db
        with db.transaction() as connection:
            rows = connection.execute(
                "SELECT strategy_key,status,max_live_weight,approved_by,approved_at FROM quant.strategy_promotion_registry"
            ).fetchall()
        by_key = {row["strategy_key"]: dict(row) for row in rows}
        self.assertEqual(set(by_key), set(STRATEGY_CONTRACTS))
        for key, row in by_key.items():
            self.assertEqual(row["status"], "disabled", key)
            self.assertEqual(row["max_live_weight"], 0, key)
            self.assertIsNone(row["approved_by"], key)
            self.assertIsNone(row["approved_at"], key)

    def test_route_reports_the_seeded_fail_closed_state(self) -> None:
        from app.main import app
        from fastapi.testclient import TestClient

        # Real startup owns the shared async DB pool; other tests in this same
        # process may already have started/stopped it. Mirror test_p0_sql_and_auth's
        # no-op lifespan so this route test only exercises HTTP routing/DB reads,
        # not the full application startup sequence.
        @asynccontextmanager
        async def no_lifespan(_: object):
            yield

        original_lifespan = app.router.lifespan_context
        app.router.lifespan_context = no_lifespan
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/strategy/promotion")
        finally:
            app.router.lifespan_context = original_lifespan
        self.assertEqual(response.status_code, 200)
        body = response.json()
        by_key = {row["strategy_key"]: row for row in body["strategies"]}
        self.assertEqual(set(by_key), set(STRATEGY_CONTRACTS))
        for key, row in by_key.items():
            self.assertFalse(row["execution_eligible"], key)
            self.assertEqual(row["weight"], 0.0, key)


if __name__ == "__main__":
    unittest.main()
