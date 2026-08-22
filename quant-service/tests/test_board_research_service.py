import unittest
from datetime import date

from app.board_research_service import run
from app.request_models import BoardResearchRunRequest


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args):
        return _Result(self.rows)


class _Database:
    def __init__(self, rows):
        self.connection = _Connection(rows)

    def transaction(self):
        database = self

        class Context:
            def __enter__(self):
                return database.connection

            def __exit__(self, *_args):
                return False

        return Context()


class BoardResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_candidates_are_deduplicated_and_enriched(self):
        calls = []
        database = _Database([
            {"symbol": "000001.SZ", "name": "A", "sector_key": "board-a", "concept_label": "板块A", "limit_tag": "2板", "limit_amount": 1, "board_net_amount": 10},
            {"symbol": "000001.SZ", "name": "A", "sector_key": "board-a", "concept_label": "板块A", "limit_tag": "2板", "limit_amount": 1, "board_net_amount": 10},
            {"symbol": "000002.SZ", "name": "B", "sector_key": "board-b", "concept_label": "板块B", "limit_tag": "首板", "limit_amount": 2, "board_net_amount": 8},
        ])

        async def flow(request):
            calls.append(("flow", request.provider))
            return {"status": "completed"}

        async def candidates(request):
            calls.append(("candidates", request.top_concepts, request.leaders_per_concept))
            return {"status": "completed", "trade_date": "2026-08-21", "concepts": [{"sector_key": "board-a"}, {"sector_key": "board-b"}]}

        async def announcements(request):
            calls.append(("announcements", request.symbols))
            return {"status": "completed", "stored": 2}

        async def study(symbol, request):
            calls.append(("study", symbol, request.as_of_date))
            return {"symbol": symbol, "as_of_date": str(request.as_of_date), "technical": {}, "analyst": {"summary": {}}, "combined": {}, "sources": [{"status": "completed"}], "events": {"announcements": []}}

        async def run_db(action, *_args, **_kwargs):
            return action()

        result = await run(
            BoardResearchRunRequest(trade_date=date(2026, 8, 21), top_concepts=2, leaders_per_concept=2, max_stock_studies=2),
            database=database, run_database=run_db, sync_concept_signals=flow,
            sync_concept_limit_candidates=candidates, sync_announcements=announcements,
            build_stock_study=study, date_for=lambda value: date.fromisoformat(value),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["candidate"]["symbol"] for item in result["studies"]], ["000001.SZ", "000002.SZ"])
        self.assertEqual(calls[2], ("announcements", ["000001.SZ", "000002.SZ"]))
        self.assertEqual(sum(item[0] == "study" for item in calls), 2)
        self.assertFalse(result["decision_eligible"])


if __name__ == "__main__":
    unittest.main()
