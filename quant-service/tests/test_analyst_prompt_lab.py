from __future__ import annotations

import unittest
from datetime import datetime, timezone

from app.analyst_prompt_lab import PROMPT_VARIANTS, _candidate_payload
from app.paper_execution_service import _latest_local_quote


class AnalystPromptLabContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.row = {
            "source_kind": "message", "source_id": "msg-1", "source_version": "1", "content_hash": "a" * 64,
            "received_at": datetime(2026, 8, 13, 1, tzinfo=timezone.utc), "scope": "stock",
            "subject_key": "600000.SH", "subject_label": "测试股", "action": "buy", "direction": 1,
            "horizon_days": 3, "strength": 0.8, "confidence": 0.9, "conditions": {},
            "evidence_span": "明确关注", "status": "eligible",
        }

    def test_all_variants_share_source_contract_and_never_gain_live_effect(self) -> None:
        for variant in PROMPT_VARIANTS:
            payload = _candidate_payload(self.row, variant)
            self.assertEqual(payload["source"]["received_at"], self.row["received_at"].isoformat())
            self.assertEqual(payload["contract"]["strategy_effect"], "none")
            self.assertTrue(payload["contract"]["requires_human_gold_label"])

    def test_strict_action_rejects_non_stock_context(self) -> None:
        payload = _candidate_payload({**self.row, "scope": "theme", "subject_key": "ths:foo"}, "strict_action")
        self.assertFalse(payload["candidate"])

    def test_risk_variant_does_not_accept_positive_buy(self) -> None:
        payload = _candidate_payload(self.row, "risk_first")
        self.assertFalse(payload["candidate"])


class PaperQuoteEvidenceTests(unittest.TestCase):
    def test_latest_local_quote_merges_raw_hard_flags(self) -> None:
        class Row(dict):
            pass

        class Result:
            def fetchone(self):
                return Row({"source_name": "tencent_free", "price": 10, "pct_change": 0,
                            "raw": {"is_suspended": True, "at_limit_up": True}})

        class Connection:
            def execute(self, *_args, **_kwargs):
                return Result()

        quote = _latest_local_quote(Connection(), "600000.SH", datetime.now(timezone.utc))
        self.assertTrue(quote["is_suspended"])
        self.assertTrue(quote["at_limit_up"])
        self.assertEqual(quote["source_name"], "tencent_free")


if __name__ == "__main__":
    unittest.main()
