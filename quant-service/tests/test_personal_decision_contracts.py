"""Focused contract tests for the actual-portfolio decision domain."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from pydantic import ValidationError

from app.personal_decision_contracts import (
    BrokerPortfolioSnapshotInput,
    PersonalTradePlanInput,
    assemble_personal_decision_brief,
)


NOW = datetime(2026, 9, 1, 7, 15, tzinfo=timezone.utc)


def holding_plan(symbol: str = "600030.SH") -> dict:
    return {
        "plan_key": f"holding:{symbol}:20260901",
        "plan_kind": "holding",
        "symbol": symbol,
        "name": "中信证券",
        "as_of_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=1)).isoformat(),
        "action": "reduce_on_trigger",
        "reduce_trigger": "跌破盘中承接并且十分钟不能收复时减仓",
        "exit_trigger": "跌破失效位且板块同步转弱时退出",
        "stop_price": "26.80",
        "target_prices": ["29.20"],
        "max_position_pct": "20",
        "rationale": ["券商板块相对强度仍在"],
        "evidence_refs": ["market-snapshot:20260901-close"],
    }


class PersonalDecisionContractTests(unittest.TestCase):
    def test_exact_snapshot_rejects_duplicate_symbols_and_unreconciled_value(self) -> None:
        payload = {
            "account_key": "citics-primary",
            "source": "citics_mumu",
            "source_snapshot_key": "20260901T151500+0800",
            "observed_at": NOW.isoformat(),
            "verification": "verified_exact",
            "total_market_value": "2000",
            "positions": [{
                "symbol": "600030.SH", "name": "中信证券", "quantity": "100",
                "sellable_quantity": "100", "market_value": "1900",
            }],
        }
        with self.assertRaisesRegex(ValidationError, "reconcile"):
            BrokerPortfolioSnapshotInput.model_validate(payload)
        payload["total_market_value"] = "1900"
        payload["positions"].append(dict(payload["positions"][0]))
        with self.assertRaisesRegex(ValidationError, "at most once"):
            BrokerPortfolioSnapshotInput.model_validate(payload)

    def test_snapshot_requires_timezone_and_sellable_not_above_quantity(self) -> None:
        payload = {
            "account_key": "citics-primary", "source": "citics_mumu",
            "source_snapshot_key": "snapshot-1", "observed_at": "2026-09-01T15:15:00",
            "verification": "verified_partial",
            "positions": [{
                "symbol": "600030.SH", "name": "中信证券", "quantity": "100",
                "sellable_quantity": "200",
            }],
        }
        with self.assertRaises(ValidationError) as raised:
            BrokerPortfolioSnapshotInput.model_validate(payload)
        self.assertIn("timezone", str(raised.exception))
        self.assertIn("must not exceed", str(raised.exception))

    def test_new_buy_plan_requires_complete_execution_fields(self) -> None:
        payload = holding_plan("600176.SH") | {
            "plan_key": "new-buy:600176.SH:20260901", "plan_kind": "new_buy",
            "name": "中国巨石", "action": "buy_on_trigger", "entry_zone": None,
            "stop_price": None, "target_prices": [], "max_position_pct": "0",
        }
        with self.assertRaisesRegex(ValidationError, "new-buy plan is incomplete"):
            PersonalTradePlanInput.model_validate(payload)

    def test_market_and_new_buy_remain_available_when_portfolio_is_missing(self) -> None:
        buy = holding_plan("600176.SH") | {
            "plan_key": "new-buy:600176.SH:20260901", "plan_kind": "new_buy",
            "name": "中国巨石", "action": "buy_on_trigger",
            "entry_zone": {"lower": "25.20", "upper": "25.60"},
            "stop_price": "24.70", "target_prices": ["26.80"], "max_position_pct": "10",
        }
        brief = assemble_personal_decision_brief(
            as_of_at=NOW,
            market_section={"status": "ready", "regime": "rotation"},
            portfolio=None,
            plans=[buy],
        )
        self.assertEqual(brief["status"], "partial")
        self.assertTrue(brief["delivery"]["market_eligible"])
        self.assertFalse(brief["delivery"]["holding_actions_eligible"])
        self.assertTrue(brief["delivery"]["new_buy_actions_eligible"])
        self.assertEqual(brief["new_buys"]["actions"][0]["symbol"], "600176.SH")

    def test_missing_holding_plan_is_diagnostic_and_not_human_facing_prose(self) -> None:
        portfolio = {
            "observed_at": NOW.isoformat(), "verification": "verified_exact",
            "positions": [{"symbol": "600030.SH", "name": "中信证券", "quantity": "100"}],
        }
        brief = assemble_personal_decision_brief(
            as_of_at=NOW,
            market_section={"status": "completed", "regime": "rotation"},
            portfolio=portfolio,
            plans=[],
        )
        self.assertEqual(brief["holdings"]["actions"], [])
        self.assertIn("holding_plan_missing:600030.SH", brief["diagnostics"])
        self.assertNotIn("pending", str(brief))
        self.assertNotIn("unfinished", str(brief))

    def test_exact_current_portfolio_with_plan_is_ready(self) -> None:
        portfolio = {
            "observed_at": NOW.isoformat(), "verification": "verified_exact",
            "positions": [{"symbol": "600030.SH", "name": "中信证券", "quantity": Decimal("100")}],
        }
        brief = assemble_personal_decision_brief(
            as_of_at=NOW + timedelta(minutes=5),
            market_section={"status": "ready", "regime": "rotation"},
            portfolio=portfolio,
            plans=[holding_plan()],
        )
        self.assertEqual(brief["status"], "ready")
        self.assertTrue(brief["delivery"]["holding_actions_eligible"])
        self.assertEqual(brief["holdings"]["actions"][0]["plan"]["name"], "中信证券")

    def test_weekend_snapshot_remains_current_but_old_or_future_snapshot_does_not(self) -> None:
        portfolio = {
            "observed_at": NOW.isoformat(), "verification": "verified_exact",
            "positions": [{"symbol": "600030.SH", "name": "中信证券", "quantity": "100"}],
        }
        monday = assemble_personal_decision_brief(
            as_of_at=NOW + timedelta(hours=66), market_section={"status": "ready"},
            portfolio=portfolio, plans=[holding_plan() | {"valid_until": (NOW + timedelta(days=4)).isoformat()}],
        )
        self.assertTrue(monday["delivery"]["holding_actions_eligible"])

        stale = assemble_personal_decision_brief(
            as_of_at=NOW + timedelta(days=5), market_section={"status": "ready"},
            portfolio=portfolio, plans=[],
        )
        self.assertFalse(stale["delivery"]["holding_actions_eligible"])

        future = assemble_personal_decision_brief(
            as_of_at=NOW, market_section={"status": "ready"},
            portfolio=portfolio | {"observed_at": (NOW + timedelta(hours=1)).isoformat()}, plans=[],
        )
        self.assertFalse(future["delivery"]["holding_actions_eligible"])


if __name__ == "__main__":
    unittest.main()
