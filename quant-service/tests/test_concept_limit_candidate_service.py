import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.concept_limit_candidate_service import ConceptLimitCandidateDependencies, run


class ConceptLimitCandidateServiceTests(unittest.TestCase):
    @staticmethod
    def dependencies(*, select, fetch, rows, persist_members, persist_candidates):
        return ConceptLimitCandidateDependencies(
            select_concepts=select, now_utc=lambda: datetime(2026, 8, 22, 1, tzinfo=timezone.utc),
            fetch_catalog=fetch, request=lambda **kwargs: SimpleNamespace(**kwargs), load_rows=rows,
            persist_members=persist_members, persist_candidates=persist_candidates,
            http_exception=HTTPException,
        )

    def test_rejects_super_get_before_any_catalog_or_database_work(self):
        async def unexpected(*_args, **_kwargs):
            raise AssertionError("provider work must not start")

        request = SimpleNamespace(provider="super_get", trade_date=None, top_concepts=3, leaders_per_concept=2)
        result = asyncio.run(run(request, self.dependencies(
            select=unexpected, fetch=unexpected, rows=unexpected,
            persist_members=unexpected, persist_candidates=unexpected,
        )))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("require provider=super", result["reason"])

    def test_exact_limit_pool_filter_and_partial_member_failure_are_preserved(self):
        selected_date = date(2026, 8, 21)
        concepts = [
            {"sector_key": "885001.TI", "label": "甲", "net_amount": 10},
            {"sector_key": "885002.TI", "label": "乙", "net_amount": 8},
        ]
        persisted = []

        async def select(_trade_date, limit):
            self.assertEqual(limit, 2)
            return selected_date, concepts

        async def fetch(request):
            if request.api_name == "ths_member" and request.params["ts_code"] == "885002.TI":
                raise HTTPException(status_code=502, detail="member source cooling")
            return {
                "status": "completed", "request_key": request.api_name + request.params.get("ts_code", ""),
                "provider": "tushare_super_sdk",
            }

        async def rows(key):
            if key.startswith("limit_list_ths"):
                return [
                    {"ts_code": "000001.SZ", "limit_type": "涨停池"},
                    {"ts_code": "000002.SZ", "limit_type": "炸板池"},
                    {"ts_code": "bad", "limit_type": "涨停池"},
                ]
            return [{"ts_code": "000001.SZ"}]

        async def persist_members(sector_key, rows, provider, observed_at):
            self.assertEqual(sector_key, "885001.TI")
            self.assertEqual(rows, [{"ts_code": "000001.SZ"}])
            self.assertEqual(provider, "tushare_super_sdk")
            self.assertEqual(observed_at.tzinfo, timezone.utc)
            return 1

        async def persist_candidates(*args):
            persisted.append(args)
            limit_by_symbol = args[4]
            membership_status = args[5]
            self.assertEqual(list(limit_by_symbol), ["000001.SZ"])
            self.assertEqual(membership_status["885002.TI"], "failed")
            return 1, [{"sector_key": "885001.TI", "stored": 1}]

        request = SimpleNamespace(provider="super", trade_date=selected_date, top_concepts=2, leaders_per_concept=2)
        result = asyncio.run(run(request, self.dependencies(
            select=select, fetch=fetch, rows=rows, persist_members=persist_members,
            persist_candidates=persist_candidates,
        )))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["limit_rows"], 1)
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(len(persisted), 1)

    def test_limit_pool_failure_returns_member_results_without_candidate_write(self):
        selected_date = date(2026, 8, 21)

        async def select(*_):
            return selected_date, [{"sector_key": "885001.TI", "label": "甲", "net_amount": 10}]

        async def fetch(request):
            if request.api_name == "limit_list_ths":
                raise HTTPException(status_code=502, detail="limit pool unavailable")
            return {"status": "completed", "request_key": "member", "provider": "super"}

        async def rows(_):
            return [{"ts_code": "000001.SZ"}]

        async def member(*_):
            return 1

        async def unexpected(*_):
            raise AssertionError("candidate writer must not run when limit pool fails")

        request = SimpleNamespace(provider="super", trade_date=selected_date, top_concepts=1, leaders_per_concept=1)
        result = asyncio.run(run(request, self.dependencies(
            select=select, fetch=fetch, rows=rows, persist_members=member, persist_candidates=unexpected,
        )))
        self.assertEqual(result["status"], "partial")
        self.assertIn("limit_list_ths failed", result["reason"])
        self.assertEqual(result["member_results"][0]["members"], 1)


if __name__ == "__main__":
    unittest.main()
