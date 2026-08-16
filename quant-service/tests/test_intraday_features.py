"""Pure-source safeguards for live peer breadth features."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from app.intraday_features import annotate_flow_snapshot_provenance, mapped_watchlist_peers
from app.intraday_factor_contracts import contracts_for_signal
from app.intraday_fast_quote_service import cross_source_confirmation
from app.intraday_state_machine import classify_setup_state


class MappedWatchlistPeerTests(unittest.TestCase):
    def test_cross_source_confirmation_is_pure_and_fails_closed_for_bad_inputs(self) -> None:
        observed_at = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)
        number = lambda value: float(value) if value not in (None, "") else None
        self.assertEqual(cross_source_confirmation(
            {"price": "10.00"}, {"price": "10.05", "observed_at": observed_at}, observed_at,
            number=number,
        )["status"], "confirmed")
        self.assertEqual(cross_source_confirmation(
            {"price": "10.00"}, {"price": "10.90", "observed_at": observed_at}, observed_at,
            number=number,
        )["status"], "mismatch")
        self.assertEqual(cross_source_confirmation(
            {"price": "10.00"}, {"price": "10.00", "observed_at": "not-a-time"}, observed_at,
            number=number,
        )["status"], "invalid")

    def test_flow_provenance_applies_only_to_quotes_that_consume_public_flow(self) -> None:
        quotes = {
            "000001.SZ": {"main_net_inflow": 100},
            "000002.SZ": {"main_net_inflow": None},
        }
        annotate_flow_snapshot_provenance(quotes, {"status": "cached", "age_seconds": 30}, max_age_seconds=45)
        self.assertTrue(quotes["000001.SZ"]["flow_snapshot"]["decision_eligible"])
        self.assertEqual(quotes["000001.SZ"]["flow_snapshot"]["age_seconds"], 30.0)
        self.assertNotIn("flow_snapshot", quotes["000002.SZ"])
        annotate_flow_snapshot_provenance(quotes, {"status": "cached", "age_seconds": 46}, max_age_seconds=45)
        self.assertFalse(quotes["000001.SZ"]["flow_snapshot"]["decision_eligible"])

    def test_peers_require_the_same_taxonomy_and_exact_sector_key(self) -> None:
        mapping = mapped_watchlist_peers(
            ["000001.SZ", "000002.SZ", "600000.SH", "300001.SZ"],
            [
                {"symbol": "000001.SZ", "taxonomy_key": "ths_concept_flow", "sector_key": "C001"},
                {"symbol": "000002.SZ", "taxonomy_key": "ths_concept_flow", "sector_key": "C001"},
                # Same display-like code but a different source taxonomy is
                # intentionally not a peer relation.
                {"symbol": "600000.SH", "taxonomy_key": "ths_industry", "sector_key": "C001"},
                {"symbol": "300001.SZ", "taxonomy_key": "ths_concept_flow", "sector_key": "C002"},
            ],
        )
        self.assertEqual(mapping["000001.SZ"]["peer_symbols"], ["000002.SZ"])
        self.assertEqual(mapping["000002.SZ"]["peer_symbols"], ["000001.SZ"])
        self.assertEqual(mapping["600000.SH"]["peer_symbols"], [])
        self.assertEqual(mapping["300001.SZ"]["peer_symbols"], [])
        self.assertEqual(mapping["000001.SZ"]["groups"][0]["taxonomy_key"], "ths_concept_flow")

    def test_state_machine_is_descriptive_and_requires_acceptance_evidence(self) -> None:
        continuation = classify_setup_state(
            {"symbol": "000001.SZ"},
            {"price": 11, "volume_ratio": 2.0, "main_net_inflow": 1},
            {"return_3m_pct": 1.0, "above_vwap_pct": 0.2, "minute_volume_multiple": 2.2},
            {"available_peer_count": 3, "confirming_peer_count": 2},
        )
        self.assertEqual(continuation["state"], "continuation_acceptance")
        self.assertIn("no_order", continuation["scope"])
        failure = classify_setup_state(
            {"symbol": "000001.SZ", "entry_price": 10},
            {"price": 9.7, "volume_ratio": 2.0, "main_net_inflow": -1},
            {"return_3m_pct": -0.8, "above_vwap_pct": -0.3, "minute_volume_multiple": 2.2},
            {"available_peer_count": 3, "confirming_peer_count": 0},
        )
        self.assertEqual(failure["state"], "acceptance_failure")
        constrained = classify_setup_state(
            {"symbol": "000001.SZ"}, {"price": 10}, {}, {},
            {"decision": "watch_only", "reason_codes": ["market_context_stale"]},
        )
        self.assertEqual(constrained["state"], "policy_constrained")

    def test_factor_contracts_declare_timing_without_changing_signal_score(self) -> None:
        contracts = contracts_for_signal({
            "score": 82,
            "conditions": {
                "minute_features": {"return_3m_pct": 1.2},
                "peer_context": {"requested_peer_count": 2},
                "order_book_proxy": {"status": "observed", "one_sided_30s_count": 1},
                "daily_rebound_state": {"state": "shadow_confirmed"},
            },
        })
        self.assertEqual([item["factor_key"] for item in contracts], [
            "daily_rebound_state", "exact_watchlist_peer_breadth", "minute_return_3m",
            "minute_volume_multiple", "order_book_proxy", "public_flow_proxy", "vwap_distance",
        ])
        self.assertEqual(next(item for item in contracts if item["factor_key"] == "order_book_proxy")["live_use"],
                         "attribution_only")
        self.assertTrue(all(item["live_use"] in {"evidence_only", "attribution_only"} for item in contracts))
        self.assertTrue(all(item["inference_permitted"] for item in contracts))
        self.assertTrue(all(not item["training_permitted"] for item in contracts))
        self.assertTrue(all(item["deprecated_at"] is None for item in contracts))


if __name__ == "__main__":
    unittest.main()
