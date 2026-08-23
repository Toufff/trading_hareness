from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import asyncio
import unittest

from fastapi import HTTPException

from app.async_intraday_decision_card_repository import decision_card


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    async def fetchone(self):
        return self.rows[0] if self.rows else None

    async def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, query_rows):
        self.query_rows = list(query_rows)
        self.calls = []

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        return _Result(self.query_rows.pop(0))


class _Database:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def transaction(self):
        yield self.connection


class AsyncIntradayDecisionCardRepositoryTests(unittest.TestCase):
    def test_uses_native_async_local_evidence_and_keeps_analyst_context_zero_weight(self):
        observed_at = datetime(2026, 8, 22, 2, tzinfo=timezone.utc)
        connection = _Connection([
            [{"observed_at": observed_at, "source_name": "tencent_watch", "price": 10.0,
              "pct_change": 1.2, "volume_ratio": 2.0, "turnover_rate": 3.0, "main_net_inflow": 10}],
            [{"signal_event_id": "event-1", "signal_key": "000001.SZ:entry:test", "signal_type": "entry",
              "severity": "info", "state": "confirmed", "score": 75, "observed_at": observed_at,
              "expires_at": observed_at + timedelta(minutes=5), "conditions": {}, "risk_flags": []}],
            [{"observed_at": observed_at, "payload": {"items": [{
                "sector_key": "C1", "label": "测试板块", "top_stocks": [{"symbol": "000001.SZ"}],
            }]}}],
            [{"remote_analyst_id": "a1", "remote_report_id": "r1", "summary": "看多", "sections": {}, "available_at": observed_at}],
            [{"remote_analyst_id": "a1", "subject_key": "theme", "subject_label": "主题", "direction": 1,
              "strength": 0.8, "extraction_confidence": 0.9, "available_at": observed_at,
              "remote_report_id": "r1", "evidence_key": "summary"}],
            [{"methodology_version": "v1", "status": "pending", "approved_by": None, "approved_at": None,
              "max_live_weight": 0.1, "reason": None, "evidence": {}}],
            [{"remote_analyst_id": "a1", "name": "分析师", "stock_claims": 2,
              "directional_stock_claims": 2, "neutral_stock_claims": 0, "settled_stock_outcomes": 3,
              "latest_claim_at": observed_at}],
        ])
        result = asyncio.run(decision_card(
            _Database(connection), "000001.SZ", strategy_market_state_fn=lambda items: ("risk_on", {"boards": len(items)}),
            classify_text=lambda _: (1, 0.8, 0.9), factor_version="text-v1", promotion_key="analyst_delta",
            max_approved_weight=0.1, json_safe_fn=lambda value: value, now_utc=observed_at + timedelta(seconds=1),
        ))
        self.assertEqual(result["action"], "entry_research_review")
        self.assertEqual(result["market_state"], "risk_on")
        self.assertEqual(result["analyst_context"]["role"], "research_context_only")
        self.assertEqual(result["analyst_context"]["max_live_weight"], 0.0)
        self.assertEqual(result["board_matches"][0]["sector_key"], "C1")
        self.assertEqual(len(connection.calls), 7)

    def test_missing_quote_is_local_not_found_before_follow_up_reads(self):
        connection = _Connection([[]])
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(decision_card(
                _Database(connection), "000001.SZ", strategy_market_state_fn=lambda _: ("unknown", {}),
                classify_text=lambda _: (0, 0.0, 0.0), factor_version="text-v1", promotion_key="analyst_delta",
                max_approved_weight=0.1, json_safe_fn=lambda value: value,
            ))
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(len(connection.calls), 1)


if __name__ == "__main__":
    unittest.main()
