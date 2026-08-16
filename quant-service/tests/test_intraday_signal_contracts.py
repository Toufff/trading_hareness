from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.intraday_signal_contracts import CONTRACT_VERSION, signal_contract


class IntradaySignalContractTests(unittest.TestCase):
    def test_entry_contract_has_causal_evidence_and_machine_invalidations(self) -> None:
        observed_at = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)
        signal = {
            "symbol": "000001.SZ", "signal_key": "000001.SZ:entry:countertrend_rebound_v1",
            "strategy_version": "countertrend-rebound-v1", "signal_type": "entry", "score": 82.5,
            "risk_flags": ["manual_review_required"],
            "conditions": {
                "setup": "countertrend_rebound_confirmed_plus_intraday_acceptance",
                "main_net_inflow": 1200,
                "minute_features": {"status": "ready", "return_3m_pct": 1.2},
                "peer_context": {"requested_peer_count": 2, "available_peer_count": 2, "confirming_peer_count": 2},
                "daily_rebound_state": {"state": "shadow_confirmed"},
                "policy_gate": {"version": "live-policy-gate-v1", "allow_confirmation": True,
                                "quote_source": "tencent_batched_watch_quote"},
                "factor_contract_version": "intraday-factor-contracts-v1",
            },
        }
        contract = signal_contract(signal, observed_at)
        self.assertEqual(contract["conditions"]["contract_version"], CONTRACT_VERSION)
        self.assertEqual(contract["strategy_key"], "countertrend_rebound_v1")
        self.assertEqual(contract["horizon_key"], "30m")
        self.assertEqual(contract["valid_until"], "2026-08-17T02:05:00+00:00")
        self.assertEqual(contract["expected_return"], None)
        self.assertIn("vwap_loss_with_negative_momentum", contract["invalidation_codes"])
        self.assertEqual([item["source"] for item in contract["evidence"]], [
            "tencent_batched_watch_quote", "public_flow_proxy", "intraday_minute_session",
            "point_in_time_watchlist_membership", "prior_completed_daily_rebound_state",
        ])
        self.assertEqual(signal["score"], 82.5)

    def test_data_issue_has_no_return_horizon(self) -> None:
        contract = signal_contract({
            "symbol": "000001.SZ", "signal_key": "000001.SZ:data_issue:watchlist-confirmation-v4",
            "signal_type": "data_issue", "score": 0, "conditions": {}, "risk_flags": [],
        }, datetime(2026, 8, 17, 2, tzinfo=timezone.utc))
        self.assertIsNone(contract["horizon_key"])
        self.assertEqual(contract["invalidation_codes"], ["quote_evidence_unavailable"])


if __name__ == "__main__":
    unittest.main()
