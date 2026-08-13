from datetime import datetime, timezone, timedelta
from decimal import Decimal
import unittest

from app.paper_execution import estimate_cost, paper_tradability, round_lot, triple_barrier_label
from app.strategy_contracts import EvidenceRef, SignalSpec, contract_payload


class PaperExecutionTests(unittest.TestCase):
    def test_round_lot_and_t_plus_one_are_conservative(self):
        self.assertEqual(round_lot(249), 200)
        result = paper_tradability(side="sell", requested_quantity=100, quote={"price": 10},
                                   position={"sellable_quantity": 0})
        self.assertFalse(result.allowed)
        self.assertIn("t_plus_one_or_insufficient_sellable_quantity", result.reasons)

    def test_limit_and_cost_model(self):
        result = paper_tradability(side="buy", requested_quantity=100,
                                   quote={"pct_change": 10.0, "price": 10})
        self.assertFalse(result.allowed)
        costs = estimate_cost(side="sell", quantity=100, price=Decimal("10"))
        self.assertEqual(costs["notional"], Decimal("1000"))
        self.assertGreater(costs["total_cost"], Decimal("5"))

    def test_triple_barrier_is_point_in_time_and_matures(self):
        start = datetime(2026, 8, 14, 1, 30, tzinfo=timezone.utc)
        spec = type("Spec", (), {"upper_return": 0.03, "lower_return": -0.02, "max_horizon_minutes": 60})
        path = [{"observed_at": start + timedelta(minutes=5), "close": 103}]
        labeled = triple_barrier_label(path, entry_price=100, entry_at=start, spec=spec)
        self.assertEqual(labeled["label"], "upper")

    def test_contract_payload_is_json_safe(self):
        observed = datetime(2026, 8, 14, tzinfo=timezone.utc)
        signal = SignalSpec("watchlist-confirmation", "v1", "watch", "000001.SZ", 1, observed,
                            evidence=(EvidenceRef("tencent", observed_at=observed),))
        payload = contract_payload(signal)
        self.assertEqual(payload["symbol"], "000001.SZ")
        self.assertEqual(payload["evidence"][0]["observed_at"], observed.isoformat())


if __name__ == "__main__":
    unittest.main()
