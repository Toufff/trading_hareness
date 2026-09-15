from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
import unittest

from app.stock_workbench_contracts import strategy_catalog
from app.stock_workbench_indicators import enrich_daily_bars
from app.stock_workbench_service import StockWorkbenchDependencies, build, parse_longhu_history


def history_envelope(count: int = 40) -> dict:
    dates = [f"2026{7 + (index // 28):02d}{index % 28 + 1:02d}" for index in range(count)]
    values = []
    for index in range(count):
        close = 10 + index * 0.05
        values.append([close - 0.03, close, close + 0.12, close - 0.15])
    return {
        "calls": 1, "physical_batch_limit": 300,
        "pages": [{"payload": {
            "x": dates, "y": values, "vol": [1000 + index for index in range(count)],
            "bal": [1_000_000 + index * 10_000 for index in range(count)],
            "turnover": [2.0] * count,
        }}],
    }


class WorkbenchIndicatorTests(unittest.TestCase):
    def test_flat_wilder_rsi_is_neutral_and_missing_ohlc_is_not_fabricated(self) -> None:
        rows = [
            {"date": f"2026-07-{index + 1:02d}", "open": 10, "high": 10, "low": 10, "close": 10, "amount": 1_000}
            for index in range(20)
        ]
        rows.insert(5, {"date": "2026-07-21", "close": 10, "amount": 1_000})
        result = enrich_daily_bars(rows)
        self.assertEqual(len(result["bars"]), 20)
        self.assertIn("2026-07-21", result["gaps"])
        self.assertEqual(result["bars"][-1]["rsi14"], 50.0)

    def test_history_parser_uses_documented_ohlc_order(self) -> None:
        rows, health = parse_longhu_history(history_envelope(2))
        self.assertEqual(rows[0]["open"], 9.97)
        self.assertEqual(rows[0]["close"], 10.0)
        self.assertEqual(rows[0]["high"], 10.12)
        self.assertEqual(rows[0]["low"], 9.85)
        self.assertEqual(health["status"], "ready")

    def test_strategy_contracts_select_data_instead_of_enabling_every_panel(self) -> None:
        contracts = {item["key"]: item for item in strategy_catalog()}
        self.assertIn("vendor_flow", contracts["accumulation"]["required_panels"])
        self.assertNotIn("messages", contracts["accumulation"]["required_panels"])
        self.assertIn("messages", contracts["event"]["required_panels"])
        self.assertNotIn("vendor_flow", contracts["event"]["required_panels"])
        self.assertEqual(contracts["event"]["metrics"][0], "volume")
        self.assertEqual(contracts["trend"]["metrics"], ("macd", "rsi", "volume"))
        self.assertIn("capital", contracts["event"]["message_categories"])


class WorkbenchServiceTests(unittest.TestCase):
    def test_build_returns_all_strategy_views_and_never_invents_buy_permission(self) -> None:
        async def run_provider(action, *args, **kwargs):
            kwargs.pop("timeout_seconds")
            return action(*args, **kwargs)

        class Source:
            def raw_call(self, _request):
                return history_envelope(40)

        async def evidence(_symbol, _as_of_date, **_kwargs):
            return {
                "instrument": {"symbol": "600000.SH", "name": "测试股份", "industry": "测试"},
                "flows": [{"trading_date": date(2026, 8, day), "net_amount": 10_000 * day,
                           "source": "longhuvip_main_net", "provider": "longhuvip_composite"} for day in range(24, 29)],
                "fundamentals": [], "events": [], "active_plan": None, "sectors": [],
                "market": [{"trading_date": date(2026, 8, day), "median_change_pct": 0.1,
                            "mean_change_pct": 0.2, "stock_count": 5000} for day in range(1, 29)],
                "regime": None, "sentiment": None,
            }

        result = asyncio.run(build(
            "600000.SH", SimpleNamespace(as_of_date=date(2026, 8, 28), lookback_days=120),
            StockWorkbenchDependencies(
                china_today=lambda: date(2026, 8, 28), run_provider=run_provider,
                evidence=evidence, source_factory=Source,
            ),
        ))
        self.assertEqual(result["name"], "测试股份")
        self.assertEqual(len(result["strategy_views"]), 10)
        self.assertEqual(result["strategy_views"][0]["key"], "user_tracking")
        self.assertFalse(result["tracking_analysis"]["buy_authorized"])
        self.assertEqual({row["key"] for row in result["strategy_views"]}, {
            "accumulation", "expansion", "pullback", "trend", "event", "relay",
            "contraction", "rotation", "reclaim", "user_tracking",
        })
        self.assertTrue(all(view["probability_status"] == "not_calibrated" for view in result["strategy_views"]))
        self.assertIsNone(result["active_trade_plan"])
        self.assertTrue(all("不自动获得买入权限" in view["next_session"][0]["action"] for view in result["strategy_views"]))
        event_view = next(view for view in result["strategy_views"] if view["key"] == "event")
        self.assertEqual(event_view["status"], "degraded")
        self.assertIn("messages", event_view["blockers"])
        accumulation_view = next(view for view in result["strategy_views"] if view["key"] == "accumulation")
        self.assertIn("5日同口径资金", accumulation_view["next_session"][0]["condition"])

    def test_stale_trade_plan_is_removed_instead_of_drawn_on_newer_bars(self) -> None:
        async def run_provider(action, *args, **kwargs):
            kwargs.pop("timeout_seconds")
            return action(*args, **kwargs)

        class Source:
            def raw_call(self, _request):
                return history_envelope(40)

        async def evidence(_symbol, _as_of_date, **_kwargs):
            return {
                "instrument": {"symbol": "600000.SH", "name": "测试股份", "industry": "测试"},
                "flows": [], "fundamentals": [], "events": [], "sectors": [], "market": [],
                "regime": None, "sentiment": None,
                "active_plan": {
                    "as_of_at": datetime(2026, 7, 1, 15, 10).isoformat(),
                    "valid_until": datetime(2026, 9, 30, 15, 10).isoformat(),
                    "stop_price": "9.00", "target_prices": ["12.00"],
                },
            }

        result = asyncio.run(build(
            "600000.SH", SimpleNamespace(as_of_date=date(2026, 8, 12), lookback_days=120),
            StockWorkbenchDependencies(
                china_today=lambda: date(2026, 8, 12), run_provider=run_provider,
                evidence=evidence, source_factory=Source,
            ),
        ))
        self.assertIsNone(result["active_trade_plan"])
        self.assertEqual(result["artifact_freshness"]["trade_plan"]["status"], "stale")
        self.assertEqual(result["data_health"]["trade_plan"]["status"], "stale")
        self.assertEqual(result["data_health"]["messages"]["status"], "unavailable")

    def test_observed_sector_and_newest_message_drive_display_freshness(self) -> None:
        async def run_provider(action, *args, **kwargs):
            kwargs.pop("timeout_seconds")
            return action(*args, **kwargs)

        class Source:
            def raw_call(self, _request):
                return history_envelope(40)

        async def evidence(_symbol, _as_of_date, **_kwargs):
            return {
                "instrument": {"symbol": "600664.SH", "name": "哈药股份", "industry": None},
                "flows": [], "fundamentals": [], "active_plan": None, "market": [],
                "regime": None, "sentiment": None,
                "sectors": [{"label": "化学制药", "trading_date": date(2026, 8, 28)}],
                # Repository order is oldest first; the presentation reverses it.
                "events": [
                    {"title": "旧公告", "available_at": datetime(2026, 7, 24, 0, 0), "occurred_at": datetime(2026, 7, 24, 0, 0)},
                    {"title": "新公告", "available_at": datetime(2026, 8, 26, 0, 0), "occurred_at": datetime(2026, 8, 26, 0, 0)},
                ],
            }

        result = asyncio.run(build(
            "600664.SH", SimpleNamespace(as_of_date=date(2026, 8, 28), lookback_days=120),
            StockWorkbenchDependencies(
                china_today=lambda: date(2026, 8, 28), run_provider=run_provider,
                evidence=evidence, source_factory=Source,
            ),
        ))
        self.assertEqual(result["industry"], "化学制药")
        self.assertEqual(result["messages"][0]["title"], "新公告")
        self.assertEqual(result["artifact_freshness"]["messages"]["latest_available_at"], "2026-08-26T00:00:00")


if __name__ == "__main__":
    unittest.main()
