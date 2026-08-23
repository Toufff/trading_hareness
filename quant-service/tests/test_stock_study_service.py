from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
import unittest

from app.stock_study_service import StockStudyDependencies, build


class StockStudyServiceTests(unittest.TestCase):
    @staticmethod
    def dependencies(*, realtime_active: bool, fetched_requests: list[object]) -> StockStudyDependencies:
        def request(**kwargs):
            payload = SimpleNamespace(**kwargs)
            fetched_requests.append(payload)
            return payload

        async def fetch_tushare(label, payload):
            if payload.api_name == "daily" and payload.provider == "primary":
                rows = [{"trade_date": "20260821", "close": 10.0}]
            elif payload.api_name == "stock_basic":
                rows = [{"ts_code": "000001.SZ", "name": "测试"}]
            elif payload.api_name == "rt_min":
                rows = [{"time": "2026-08-21 10:00:00", "close": 10.1}]
            else:
                rows = [{"trade_date": "20260821", "api": payload.api_name}]
            return ({"source": label, "api_name": payload.api_name, "provider": getattr(payload, "provider", "auto"),
                     "status": "completed", "received": len(rows), "stored": len(rows)}, rows)

        async def free_fetch(label, provider, capability, fetcher, _symbol):
            payload = await fetcher()
            return ({"source": label, "api_name": capability, "provider": provider, "status": "completed",
                     "received": len(payload) if isinstance(payload, list) else int(bool(payload)), "stored": 1}, payload)

        async def run_database(action, *args, **_kwargs):
            return action(*args)

        async def session():
            return realtime_active, "continuous_auction" if realtime_active else "market_closed"

        async def baostock(_request):
            return {"status": "completed", "imported": 1, "failures": []}

        async def daily(*_args):
            return [{"trade_date": "20260821", "close": 10.0}]

        async def quote(*_args):
            return {"price": 10.0}

        async def run_akshare(action, *args, **_kwargs):
            return action(*args)

        def akshare_daily(*_args):
            return [{"trade_date": "20260821", "close": 10.0}]

        async def announcements(*_args, **_kwargs):
            return [{"ts_code": "000001.SZ", "title": "公告"}]

        return StockStudyDependencies(
            china_today=lambda: date(2026, 8, 21), tushare_request=request, daily_sync_request=request,
            fetch_tushare=fetch_tushare, realtime_market_session=session, sync_baostock=baostock,
            free_fetch=free_fetch, eastmoney_daily=daily, eastmoney_quote=quote, run_akshare=run_akshare,
            akshare_daily=akshare_daily, tencent_daily=daily, sina_quote=quote,
            cninfo_announcements=announcements, run_database=run_database,
            persist_market_events=lambda _provider, rows: len(rows), persist_announcement_health=lambda *_args: None,
            technical_summary=lambda _rows: {"score": 70, "reasons": ["trend"]},
            analyst_claims=lambda _symbol: ([{"id": 1}], {"score": 0.4, "claim_count": 1, "direction": "positive"}),
            recent_events=lambda _symbol, _limit: [{"title": "公告"}],
            window_readiness=lambda _symbol, _start, _end: {"decision_ready": False, "blockers": ["daily_basic"]},
            latest_row=lambda rows: rows[-1] if rows else None,
        )

    def test_closed_session_keeps_realtime_inputs_explicitly_skipped(self) -> None:
        fetched_requests: list[object] = []
        request = SimpleNamespace(as_of_date=date(2026, 8, 22), lookback_days=21)

        result = asyncio.run(build("000001.SZ", request, self.dependencies(realtime_active=False, fetched_requests=fetched_requests)))

        self.assertEqual(result["as_of_date"], "2026-08-21")
        self.assertFalse(any(getattr(item, "api_name", None) == "rt_min" for item in fetched_requests))
        skipped = [item for item in result["sources"] if item["api_name"] == "rt_min"]
        self.assertEqual([item["status"] for item in skipped], ["skipped", "skipped"])
        self.assertEqual(result["events"]["decision_eligible"], False)
        self.assertEqual(result["market"]["tencent_daily_bars"][0]["close"], 10.0)

    def test_live_session_adds_both_realtime_adapters_without_changing_research_boundary(self) -> None:
        fetched_requests: list[object] = []
        request = SimpleNamespace(as_of_date=date(2026, 8, 21), lookback_days=21)

        result = asyncio.run(build("000001.SZ", request, self.dependencies(realtime_active=True, fetched_requests=fetched_requests)))

        realtime_requests = [item for item in fetched_requests if getattr(item, "api_name", None) == "rt_min"]
        self.assertEqual([item.provider for item in realtime_requests], ["primary", "super"])
        self.assertEqual(result["market"]["latest_realtime"]["close"], 10.1)
        self.assertIn("不构成交易指令", result["combined"]["notice"])
