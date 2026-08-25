from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.fuyao_catalog import FUYAO_PATHS, catalog_contract, catalog_items
from app.fuyao_provider import (
    FuyaoProviderError,
    FuyaoQueryValidationError,
    configured,
    fetch_envelope,
    normalize_snapshot_rows,
    validate_capability_query,
)
from app.request_models import FuyaoQueryRequest
from app.routers.provider_actions import ProviderActionDependencies, build_provider_actions_router


OFFICIAL_REST_PATHS = frozenset("""
/api/a-share-index/catalog/ths-index-list
/api/a-share-index/constituents/ths-stock-list
/api/a-share-index/prices/historical
/api/a-share-index/prices/snapshot
/api/a-share/auction/short-term-benchmark
/api/a-share/auction/snapshot
/api/a-share/calendar/trading-days
/api/a-share/corporate-actions/adjustment-factors
/api/a-share/financials/balance-sheets
/api/a-share/financials/cash-flow-statements
/api/a-share/financials/income-statements
/api/a-share/financials/indicators
/api/a-share/prices/historical
/api/a-share/prices/snapshot
/api/a-share/special-data/anomaly-analysis-list
/api/a-share/special-data/anomaly-analysis-stock
/api/a-share/special-data/dragon-tiger-list
/api/a-share/special-data/hot-stock-list
/api/a-share/special-data/hot-stock-list-history
/api/a-share/special-data/hot-stock-rank-trend
/api/a-share/special-data/limit-break-pool
/api/a-share/special-data/limit-down-pool
/api/a-share/special-data/limit-up-ladder
/api/a-share/special-data/limit-up-pool
/api/a-share/special-data/skyrocket-list
/api/a-share/valuations/snapshot
/api/dump/market-dumps/adjustment-factors/download-url
/api/dump/market-dumps/daily-k-10d/download-url
/api/dump/market-dumps/daily-k/download-url
/api/fund/companies/detail
/api/fund/corporate-actions/dividends
/api/fund/diagnostics/detail
/api/fund/financials/balance-sheets
/api/fund/financials/income-statements
/api/fund/financials/indicators
/api/fund/holders/detail
/api/fund/holders/top
/api/fund/managers/detail
/api/fund/managers/experience
/api/fund/managers/investment-style
/api/fund/managers/performance
/api/fund/market/historical
/api/fund/market/snapshot
/api/fund/news/article-list
/api/fund/offerings/list
/api/fund/performance/drawdowns
/api/fund/performance/indicators-historical
/api/fund/performance/nav
/api/fund/performance/returns
/api/fund/portfolio/asset-allocation
/api/fund/portfolio/bond-history
/api/fund/portfolio/bond-report-dates
/api/fund/portfolio/holdings
/api/fund/portfolio/industry-allocation
/api/fund/portfolio/stock-history
/api/fund/portfolio/stock-report-dates
/api/fund/profile/detail
/api/meta/tickers/list
/api/meta/tickers/search
""".split())


class FuyaoProviderTests(unittest.TestCase):
    def test_normalizes_official_snapshot_without_inventing_flow_fields(self) -> None:
        rows = normalize_snapshot_rows({
            "timestamp": 1787625155000,
            "item": [{
                "thscode": "600519.SH", "last_price": 1305.99,
                "prev_price": 1304.66, "price_change_ratio_pct": 0.101942,
                "volume": 1006124, "turnover": 1316263770,
            }],
        })
        self.assertEqual(rows[0]["symbol"], "600519.SH")
        self.assertEqual(rows[0]["price_source"], "fuyao_ths_all_a_snapshot")
        self.assertEqual(rows[0]["turnover"], 1316263770.0)
        self.assertNotIn("main_net_inflow", rows[0])

    def test_capability_query_is_allowlisted_and_scalar(self) -> None:
        self.assertEqual(
            validate_capability_query("a_share_prices_snapshot", {"thscodes": "600519.SH", "limit": 100}),
            "/api/a-share/prices/snapshot",
        )
        with self.assertRaises(FuyaoProviderError):
            validate_capability_query("not_a_capability", {})
        with self.assertRaises(FuyaoProviderError):
            validate_capability_query("a_share_prices_snapshot", {"nested": {"not": "allowed"}})

    def test_catalog_exactly_covers_the_official_59_rest_paths(self) -> None:
        contract = catalog_contract()
        self.assertEqual(len(FUYAO_PATHS), 59)
        self.assertEqual(len(set(FUYAO_PATHS.values())), 59)
        self.assertEqual(set(FUYAO_PATHS.values()), OFFICIAL_REST_PATHS)
        self.assertTrue(contract["rest_contract_complete"])
        self.assertTrue(contract["parameter_contract_complete"])
        self.assertEqual(
            FUYAO_PATHS["a_share_financial_indicators"],
            "/api/a-share/financials/indicators",
        )
        self.assertEqual(
            FUYAO_PATHS["a_share_valuations_snapshot"],
            "/api/a-share/valuations/snapshot",
        )
        self.assertTrue(all("allowed_params" in item and "required_params" in item for item in catalog_items()))

    def test_query_contract_rejects_unknown_and_missing_parameters(self) -> None:
        with self.assertRaisesRegex(FuyaoProviderError, "unknown Fuyao query parameters: typo"):
            validate_capability_query("a_share_valuations_snapshot", {"thscodes": "600519.SH", "typo": 1})
        with self.assertRaisesRegex(FuyaoProviderError, "missing Fuyao query parameters: report"):
            validate_capability_query("a_share_financial_indicators", {"thscode": "600519.SH"})

    def test_canonical_environment_name_is_supported(self) -> None:
        with patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": "present"}, clear=True):
            self.assertTrue(configured())

    def test_fetch_envelope_preserves_request_id(self) -> None:
        response = MagicMock(status_code=200, headers={})
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "code": 0, "message": "success", "request_id": "request-for-support",
            "data": {"timestamp": 1, "item": []},
        }
        client = MagicMock()
        client.get = AsyncMock(return_value=response)

        @asynccontextmanager
        async def fake_client(*_args: object, **_kwargs: object):
            yield client

        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": "secret"}, clear=True), \
                 patch("app.fuyao_provider.provider_http_client", new=fake_client):
                return await fetch_envelope("a_share_valuations_snapshot", {"thscodes": "600519.SH"})

        envelope = asyncio.run(run())
        self.assertEqual(envelope["request_id"], "request-for-support")
        self.assertEqual(envelope["data"], {"timestamp": 1, "item": []})
        _, call_kwargs = client.get.call_args
        self.assertEqual(call_kwargs["headers"], {"X-api-key": "secret"})

    def test_fetch_envelope_retries_documented_transient_business_code(self) -> None:
        transient = MagicMock(status_code=200, headers={})
        transient.raise_for_status.return_value = None
        transient.json.return_value = {
            "code": 5001, "message": "temporary", "request_id": "first", "data": None,
        }
        success = MagicMock(status_code=200, headers={})
        success.raise_for_status.return_value = None
        success.json.return_value = {
            "code": 0, "message": "success", "request_id": "second", "data": {"item": []},
        }
        client = MagicMock()
        client.get = AsyncMock(side_effect=[transient, success])

        @asynccontextmanager
        async def fake_client(*_args: object, **_kwargs: object):
            yield client

        async def run() -> dict[str, object]:
            with patch.dict(os.environ, {"HITHINK_FINANCE_API_KEY": "secret"}, clear=True), \
                 patch("app.fuyao_provider.provider_http_client", new=fake_client), \
                 patch("app.fuyao_provider.asyncio.sleep", new=AsyncMock()) as sleep:
                result = await fetch_envelope("a_share_trading_days", {})
                self.assertEqual(sleep.await_count, 1)
                return result

        envelope = asyncio.run(run())
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(envelope["request_id"], "second")

    def test_router_maps_local_query_validation_to_422(self) -> None:
        noop = AsyncMock(return_value={})
        fuyao = AsyncMock(side_effect=FuyaoQueryValidationError("unknown Fuyao query parameters: typo"))
        router = build_provider_actions_router(ProviderActionDependencies(
            akshare_probe=noop, realtime_probe=noop, tushare_audit=noop,
            tushare_fetch=noop, stock_study=noop, fuyao_query=fuyao,
        ))
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/v1/providers/fuyao/query")

        async def run() -> None:
            with self.assertRaises(HTTPException) as caught:
                await endpoint(FuyaoQueryRequest(capability="a_share_trading_days", params={}))
            self.assertEqual(caught.exception.status_code, 422)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
