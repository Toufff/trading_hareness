import asyncio
from datetime import date
from types import SimpleNamespace
import unittest

from app.ths_concept_member_backfill_service import ThsConceptMemberBackfillDependencies, run


class ThsConceptMemberBackfillServiceTests(unittest.TestCase):
    @staticmethod
    def dependencies(*, existing, flow, members, progress):
        return ThsConceptMemberBackfillDependencies(
            china_today=lambda: date(2026, 8, 22), load_existing_flow=existing,
            sync_flow_catalog=flow, flow_request=lambda **kwargs: SimpleNamespace(**kwargs),
            sync_members=members, member_request=lambda **kwargs: SimpleNamespace(**kwargs),
            load_progress=progress,
        )

    def test_blocks_before_member_request_when_flow_catalog_is_unavailable(self):
        async def existing(_):
            return {"rows": 0}

        async def flow(_):
            return {"sources": {"concept_flow": {"status": "failed"}}}

        async def unexpected(_):
            raise AssertionError("member hydration must not run without a flow universe")

        request = SimpleNamespace(trade_date=date(2026, 8, 21), refresh_flow_catalog=False, provider="super", batch_size=10)
        result = asyncio.run(run(request, self.dependencies(existing=existing, flow=flow, members=unexpected, progress=unexpected)))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("not guessed", result["reason"])

    def test_refreshes_then_returns_durable_progress_for_exact_member_batch(self):
        calls = []

        async def existing(value):
            calls.append(("existing", value))
            return {"rows": 4}

        async def flow(request):
            calls.append(("flow", request.trade_date, request.provider))
            return {"sources": {"concept_flow": {"status": "partial"}}}

        async def members(request):
            calls.append(("members", request.member_limit, request.resume))
            return {"status": "completed", "total_concepts": 8}

        async def progress(value):
            calls.append(("progress", value))
            return {"done": 6, "failed": 1}

        request = SimpleNamespace(trade_date=date(2026, 8, 21), refresh_flow_catalog=True, provider="super_sdk", batch_size=12)
        result = asyncio.run(run(request, self.dependencies(existing=existing, flow=flow, members=members, progress=progress)))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress"], {"completed_or_empty": 6, "failed": 1, "remaining": 2})
        self.assertEqual([item[0] for item in calls], ["existing", "flow", "members", "progress"])

    def test_member_sync_blocked_has_explicit_unknown_remaining_progress(self):
        async def existing(_):
            return {"rows": 1}

        async def unexpected(_):
            raise AssertionError("flow/progress should not run")

        async def members(_):
            return {"status": "blocked", "reason": "provider cooling"}

        request = SimpleNamespace(trade_date=date(2026, 8, 21), refresh_flow_catalog=False, provider="super", batch_size=10)
        result = asyncio.run(run(request, self.dependencies(existing=existing, flow=unexpected, members=members, progress=unexpected)))
        self.assertEqual(result["progress"]["remaining"], None)
        self.assertEqual(result["progress"]["completed_or_empty"], 0)


if __name__ == "__main__":
    unittest.main()
