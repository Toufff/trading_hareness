from __future__ import annotations

from datetime import date, datetime, timezone
import unittest

from app.limit_linkage_mining_service import LimitLinkageMiningDependencies, run


class LimitLinkageMiningServiceTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def dependencies(*, relations, persist):
        async def load(_):
            return relations

        def select(rows, quotes):
            return ([{"symbol": "000001.SZ"}], {"exact_relation_rows": len(rows), "candidate_count": 1})

        return LimitLinkageMiningDependencies(
            trade_date=lambda _: date(2026, 8, 21), load_relations=load,
            select_candidates=select, persist=persist, safe_error=lambda value, _: value,
        )

    async def test_blocks_without_exact_same_day_relations_before_persisting(self) -> None:
        async def unexpected(*_):
            raise AssertionError("empty exact relations must not create a run")

        result = await run(datetime(2026, 8, 21, 2, tzinfo=timezone.utc), {}, self.dependencies(relations=[], persist=unexpected))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exact THS concept membership", result["reason"])

    async def test_persists_only_selected_exact_relation_candidates(self) -> None:
        received = []

        async def persist(observed, trade_date, candidates, summary):
            received.extend((observed, trade_date, candidates, summary))
            return "run-1"

        observed_at = datetime(2026, 8, 21, 2, tzinfo=timezone.utc)
        result = await run(observed_at, {"000001.SZ": {}}, self.dependencies(relations=[{"symbol": "000001.SZ"}], persist=persist))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["linkage_run_id"], "run-1")
        self.assertEqual(received[0], observed_at)
        self.assertEqual(received[1], date(2026, 8, 21))
        self.assertEqual(received[2], [{"symbol": "000001.SZ"}])


if __name__ == "__main__":
    unittest.main()
