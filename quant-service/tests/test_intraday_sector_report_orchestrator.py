from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

from app.intraday_sector_report_orchestrator import run


class _Saturated(Exception):
    pass


class _Provider(Exception):
    pass


class IntradaySectorReportOrchestratorTests(IsolatedAsyncioTestCase):
    async def test_transient_executor_block_is_fail_closed(self):
        async def blocked(*_args, **_kwargs):
            raise _Saturated("executor full")

        result = await run(
            SimpleNamespace(kind="all", hydrate_top_boards=0, top_stocks=10),
            run_public_blocking=blocked, board_flow=lambda kind: kind, all_a_snapshot=blocked,
            build_membership_report=blocked, hydrate_members=blocked, member_symbol=lambda _: None,
            number=lambda value: None, exchange_date=lambda: None, safe_error=lambda value, limit: value[:limit],
            executor_saturated_error=_Saturated, provider_error=_Provider,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["sources"]["eastmoney"], "not_started")

    async def test_completed_report_keeps_runtime_quotes_out_of_contract_semantics(self):
        async def fetch(fn, *args, **_kwargs):
            return [{"板块代码": args[0], "板块名称": "PCB", "流入资金": 8, "流出资金": 3}]

        async def build(*_args):
            return ([{"taxonomy_key": "eastmoney_concept", "net_inflow": 5, "label": "PCB"}], {"concept": {"flow_boards": 1}}, [], [], [])

        async def hydrate(*_args):
            return []

        async def all_a_snapshot():
            return [{"symbol": "600000.SH", "pct_change": "2", "turnover": "6"}], {"status": "fresh"}
        result = await run(
            SimpleNamespace(kind="concept", hydrate_top_boards=0, top_stocks=10),
            run_public_blocking=fetch, board_flow=lambda kind: object(), all_a_snapshot=all_a_snapshot,
            build_membership_report=build, hydrate_members=hydrate, member_symbol=lambda _: None,
            number=lambda value: float(value) if value is not None else None, exchange_date=lambda: None,
            safe_error=lambda value, limit: value[:limit], executor_saturated_error=_Saturated, provider_error=_Provider,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["_runtime_quotes"]["600000.SH"]["turnover"], 6.0)
        self.assertFalse(result["decision_eligible"])

    async def test_member_hydration_timeout_degrades_only_that_context(self):
        async def fetch(_fn, kind, **_kwargs):
            return [{"kind": kind}]

        async def all_a_snapshot():
            return [], {"status": "fresh"}

        async def hydrate(*_args):
            await asyncio.sleep(30)
            return []

        async def build(*_args):
            return ([], {}, [], [], [])

        result = await run(
            SimpleNamespace(kind="concept", hydrate_top_boards=1, top_stocks=10),
            run_public_blocking=fetch, board_flow=lambda kind: kind, all_a_snapshot=all_a_snapshot,
            build_membership_report=build, hydrate_members=hydrate, member_symbol=lambda _: None,
            number=lambda value: value, exchange_date=lambda: None, safe_error=lambda value, limit: value[:limit],
            executor_saturated_error=_Saturated, provider_error=_Provider,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["membership_hydration_status"]["concept"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
