import asyncio
from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from app.all_board_member_backfill_service import AllBoardMemberBackfillDependencies, run


class AllBoardMemberBackfillServiceTests(unittest.TestCase):
    @staticmethod
    def dependencies(ths, eastmoney, catalog=None):
        async def catalogs():
            return {"status": "completed", "catalogs": 6}

        return AllBoardMemberBackfillDependencies(
            sync_all_ths_catalogs=catalogs, sync_ths_catalog=ths,
            ths_request=lambda **kwargs: SimpleNamespace(**kwargs),
            sync_eastmoney_members=eastmoney,
            eastmoney_request=lambda **kwargs: SimpleNamespace(**kwargs),
            http_exception=HTTPException,
        )

    def test_advances_each_exact_source_once_and_isolates_ths_failure(self):
        ths_calls, eastmoney_calls = [], []

        async def ths(request):
            ths_calls.append(request)
            if request.index_type == "R":
                raise HTTPException(status_code=502, detail="provider cooling")
            return {"status": "completed", "index_type": request.index_type}

        async def eastmoney(request):
            eastmoney_calls.append(request)
            return {"status": "partial", "kind": request.kind}

        request = SimpleNamespace(batch_size=8, refresh_catalogs=True, include_ths=True, include_eastmoney=True)
        result = asyncio.run(run(request, self.dependencies(ths, eastmoney)))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_sources"], 1)
        self.assertEqual(result["successful_sources"], 8)
        self.assertEqual([item.index_type for item in ths_calls], ["N", "I", "R", "S", "ST", "BB"])
        self.assertEqual([item.kind for item in eastmoney_calls], ["industry", "concept"])
        failed = next(item for item in result["results"] if item.get("index_type") == "R")
        self.assertEqual(failed["reason"], "provider cooling")

    def test_honors_source_flags_without_implicit_catalog_or_provider_work(self):
        async def unexpected(_):
            raise AssertionError("disabled source must not be called")

        request = SimpleNamespace(batch_size=5, refresh_catalogs=True, include_ths=False, include_eastmoney=False)
        result = asyncio.run(run(request, self.dependencies(unexpected, unexpected)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["successful_sources"], 0)


if __name__ == "__main__":
    unittest.main()
