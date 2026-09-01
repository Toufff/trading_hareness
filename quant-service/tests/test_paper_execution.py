from datetime import datetime, timezone, timedelta
from decimal import Decimal
import unittest

from app.paper_execution import estimate_cost, paper_tradability, round_lot, triple_barrier_label
from app.ashare_reality import price_limit_state
from app.paper_execution_service import configure_paper_account
from app.strategy_ablation import ablation_scores
from app.paper_portfolio import paper_risk_gate
from app.strategy_contracts import EvidenceRef, SignalSpec, contract_payload


class PaperExecutionTests(unittest.TestCase):
    def test_analyst_shadow_is_bounded_and_live_zero(self):
        scores = ablation_scores(market_signal=0.4, analyst_signal=-0.4,
                                 has_analyst_evidence=True, applied_weight=0.0)
        self.assertLess(scores["analyst_shadow_score"], scores["market_only_score"])
        self.assertEqual(scores["applied_score"], scores["market_only_score"])
        self.assertEqual(scores["shadow_weight"], 0.1)

    def test_filled_shared_paper_ledger_cannot_reset_cash(self):
        class FilledConnection:
            def execute(self, sql, params=None):
                class Result:
                    def __init__(self, row):
                        self.row = row

                    def fetchone(self):
                        return self.row
                if "SELECT cash FROM quant.paper_accounts" in sql:
                    return Result({"cash": Decimal("10000")})
                if "SELECT EXISTS(SELECT 1 FROM quant.paper_order_fills)" in sql:
                    return Result({"exists": True})
                raise AssertionError(f"unexpected SQL: {sql}")

        with self.assertRaisesRegex(ValueError, "filled activity"):
            configure_paper_account(FilledConnection(), account_key="default",
                                    initial_cash=Decimal("1000"), configured_by="test")
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

    def test_board_and_st_limit_fallback_uses_the_correct_price_band(self):
        self.assertTrue(paper_tradability(
            side="buy", requested_quantity=100, symbol="300001.SZ", quote={"pct_change": 20.0, "price": 10},
        ).allowed is False)
        self.assertTrue(paper_tradability(
            side="buy", requested_quantity=100, symbol="300001.SZ", quote={"pct_change": 10.0, "price": 10},
        ).allowed)
        self.assertFalse(paper_tradability(
            side="buy", requested_quantity=100, symbol="830001.BJ", quote={"pct_change": 30.0, "price": 10},
        ).allowed)
        self.assertFalse(paper_tradability(
            side="buy", requested_quantity=100, symbol="600001.SH", quote={"pct_change": 5.0, "price": 10, "is_st": True},
        ).allowed)

    def test_exact_limit_price_precedes_percent_fallback(self):
        state = price_limit_state(symbol="300001.SZ", quote={"price": 12, "pct_change": 10, "limit_up": 12, "limit_down": 8})
        self.assertTrue(state["at_limit_up"])
        self.assertFalse(state["at_limit_down"])

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

    def test_watch_is_not_a_virtual_position(self):
        decision = paper_risk_gate(signal_type="watch", symbol="000001.SZ")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.target_weight, 0.0)

    def test_sector_concentration_blocks_new_entry(self):
        decision = paper_risk_gate(
            signal_type="entry", symbol="000001.SZ",
            snapshot={"sector_exposure": {"semiconductor": 0.19}},
            candidate_sector_keys=["semiconductor"], max_target_weight=0.05,
            max_sector_exposure=0.20,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("sector_exposure_limit", decision.reasons)
        self.assertIn("paper_sector_exposure_block", decision.risk_flags)

    def test_drawdown_and_daily_loss_block_new_entry(self):
        decision = paper_risk_gate(
            signal_type="entry", symbol="000001.SZ",
            snapshot={"drawdown": -0.10, "daily_return": -0.04},
        )
        self.assertFalse(decision.allowed)
        self.assertIn("portfolio_drawdown_limit", decision.reasons)
        self.assertIn("paper_daily_loss_limit", decision.reasons)

    def test_mark_to_market_splits_sector_exposure(self):
        from app.paper_portfolio import mark_to_market
        snapshot = mark_to_market(
            positions=[{"symbol": "000001.SZ", "quantity": 100, "average_cost": 10,
                        "sector_keys": ["bank", "large_cap"]}],
            quotes={"000001.SZ": {"price": 10}}, cash=0,
        )
        self.assertAlmostEqual(snapshot["sector_exposure"]["bank"], 0.5)
        self.assertAlmostEqual(snapshot["sector_exposure"]["large_cap"], 0.5)

    def test_portfolio_sector_membership_is_point_in_time(self):
        from app.paper_portfolio import persist_portfolio_snapshot

        class Connection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                class Result:
                    def fetchall(self):
                        return []
                return Result()

        connection = Connection()
        persist_portfolio_snapshot(
            connection, as_of=datetime(2026, 8, 14, 0, 30, tzinfo=timezone.utc),
            quotes={}, cash=1000,
        )
        membership_sql, params = connection.calls[0]
        self.assertIn("effective_from<=%s", membership_sql)
        self.assertEqual(params, (datetime(2026, 8, 14).date(), datetime(2026, 8, 14).date(), datetime(2026, 8, 14).date()))


class RoundTripCostPercentTests(unittest.TestCase):
    """Research settles in percentages and cannot call the notional estimator.

    The percentage form has to stay derived from the same constants: a second
    rate table would drift from the paper-trading one, and every net figure in
    the scorecards would then be judged against a cost nobody trades at.
    """

    def test_it_is_one_buy_plus_one_sell_from_the_shared_constants(self):
        from app.ashare_reality import (
            DEFAULT_COMMISSION_RATE, DEFAULT_SLIPPAGE_BPS, DEFAULT_STAMP_TAX_RATE,
            round_trip_cost_pct,
        )
        slippage = DEFAULT_SLIPPAGE_BPS / Decimal("10000")
        expected = ((DEFAULT_COMMISSION_RATE + slippage)
                    + (DEFAULT_COMMISSION_RATE + DEFAULT_STAMP_TAX_RATE + slippage)) * Decimal("100")
        self.assertEqual(round_trip_cost_pct(), expected)

    def test_stamp_tax_is_charged_once_on_the_sell_leg_only(self):
        from app.ashare_reality import DEFAULT_STAMP_TAX_RATE, round_trip_cost_pct
        without_stamp = round_trip_cost_pct(stamp_tax_rate=Decimal("0"))
        self.assertEqual(round_trip_cost_pct() - without_stamp,
                         DEFAULT_STAMP_TAX_RATE * Decimal("100"))

    def test_it_agrees_with_the_notional_estimator_above_the_commission_floor(self):
        from app.ashare_reality import estimate_trade_cost, round_trip_cost_pct
        # 10,000 shares at 50 is far above the 5 yuan floor, so the two forms
        # must agree; below the floor they deliberately do not, which is why
        # the percentage form documents that it understates small positions.
        quantity, price = 10_000, Decimal("50")
        notional = price * quantity
        buy = estimate_trade_cost(side="buy", quantity=quantity, price=price)
        sell = estimate_trade_cost(side="sell", quantity=quantity, price=price)
        combined = (buy["total_cost"] + sell["total_cost"]) / notional * Decimal("100")
        self.assertAlmostEqual(float(combined), float(round_trip_cost_pct()), places=9)

    def test_the_round_trip_is_material_against_the_edges_being_measured(self):
        from app.ashare_reality import round_trip_cost_pct
        # 2026-08-27: supplement_rotation settled at +0.36% gross. If the
        # round trip ever falls below that the guard this protects is gone.
        self.assertGreater(float(round_trip_cost_pct()), 0.2)


if __name__ == "__main__":
    unittest.main()
