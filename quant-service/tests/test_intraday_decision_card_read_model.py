from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from fastapi import HTTPException

from app.intraday_decision_card_read_model import decision_card


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return _Result(self.rows.pop(0))


class IntradayDecisionCardReadModelTests(unittest.TestCase):
    def test_card_projects_only_saved_evidence_and_marks_board_match(self) -> None:
        observed_at = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)
        connection = _Connection([
            {"observed_at": observed_at, "source_name": "tencent_watch", "price": 10.0,
             "pct_change": 1.2, "volume_ratio": 2.0, "turnover_rate": 3.0, "main_net_inflow": 10},
            {"signal_event_id": "event-1", "signal_key": "000001.SZ:entry:test", "signal_type": "entry",
             "severity": "info", "state": "confirmed", "score": 75, "observed_at": observed_at,
             "expires_at": observed_at + timedelta(minutes=5), "conditions": {}, "risk_flags": ["manual_review"]},
            {"observed_at": observed_at - timedelta(seconds=20), "payload": {"items": [{
                "taxonomy_key": "ths_concept_flow", "sector_key": "C1", "label": "测试板块",
                "net_inflow": 123, "change_pct": 2.3, "top_stocks": [{"symbol": "000001.SZ"}],
            }]}},
        ])
        payload = decision_card(
            connection, "000001.SZ",
            strategy_market_state_fn=lambda items: ("risk_on", {"boards": len(items)}),
            analyst_execution_context_fn=lambda *_args: {"status": "zero_weight"},
            json_safe_fn=lambda value: value,
            now_utc=observed_at + timedelta(minutes=2),
        )
        self.assertEqual(payload["action"], "entry_research_review")
        self.assertEqual(payload["market_state"], "risk_on")
        self.assertEqual(payload["board_matches"][0]["sector_key"], "C1")
        self.assertNotIn("stale_quote_no_realtime_action", payload["risk_flags"])
        self.assertEqual(len(connection.calls), 3)

    def test_missing_saved_quote_is_a_local_not_found(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            decision_card(
                _Connection([None]), "000001.SZ",
                strategy_market_state_fn=lambda _items: ("unknown", {}),
                analyst_execution_context_fn=lambda *_args: {}, json_safe_fn=lambda value: value,
            )
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
