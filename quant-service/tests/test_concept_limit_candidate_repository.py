from __future__ import annotations

from datetime import date, datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.concept_limit_candidate_repository import persist_candidates, select_concepts


class _Transaction:
    def __init__(self, execute):
        self.execute = execute

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ConceptLimitCandidateRepositoryTests(unittest.TestCase):
    def test_select_concepts_uses_requested_date_without_querying_latest_value(self) -> None:
        calls = []

        def execute(sql, params=()):
            calls.append((str(sql), params))
            if "max(trading_date)" in str(sql):
                return MagicMock(fetchone=MagicMock(return_value={"latest": date(2026, 8, 20)}))
            return MagicMock(fetchall=MagicMock(return_value=[{"sector_key": "885001.TI", "label": "甲", "net_amount": 2}]))

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        result_date, concepts = select_concepts(database, date(2026, 8, 21), 3)

        self.assertEqual(result_date, date(2026, 8, 21))
        self.assertEqual(concepts[0]["sector_key"], "885001.TI")
        self.assertIn((date(2026, 8, 21), 3), [params for _sql, params in calls])

    def test_persist_candidates_keeps_exact_members_and_provider_provenance(self) -> None:
        statements = []
        memberships = [
            {"sector_key": "885001.TI", "symbol": "000001.SZ", "raw": {"member": "one"}},
            {"sector_key": "885001.TI", "symbol": "000002.SZ", "raw": {"member": "two"}},
            {"sector_key": "885999.TI", "symbol": "000003.SZ", "raw": {"member": "other"}},
        ]

        def execute(sql, params=()):
            statements.append((str(sql), params))
            if "sector_membership_history" in str(sql):
                return MagicMock(fetchall=MagicMock(return_value=memberships))
            return MagicMock()

        database = MagicMock()
        database.transaction.return_value = _Transaction(execute)
        observed_at = datetime(2026, 8, 22, 1, tzinfo=timezone.utc)
        stored, per_concept = persist_candidates(
            database,
            date(2026, 8, 21),
            [{"sector_key": "885001.TI", "label": "甲", "net_amount": 10}],
            ["885001.TI"],
            "tushare_super_sdk",
            {
                "000001.SZ": {"name": "甲一", "limit_amount": "5", "pct_chg": "10", "limit_type": "涨停池"},
                "000002.SZ": {"name": "甲二", "limit_amount": "8", "pct_chg": "9", "limit_type": "涨停池"},
            },
            {"885001.TI": "completed"},
            observed_at,
            1,
            lambda value: float(value) if value not in (None, "") else None,
            lambda value: value,
            lambda value: value,
        )

        self.assertEqual(stored, 1)
        self.assertEqual(per_concept, [{"sector_key": "885001.TI", "label": "甲", "net_amount": 10,
                                        "matched_limit_ups": 2, "stored": 1}])
        inserts = [params for sql, params in statements if "INSERT INTO quant.sector_limit_candidates" in sql]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1], "000002.SZ")
        self.assertEqual(inserts[0][-1]["membership_fetch_status"], "completed")
        self.assertEqual(inserts[0][-1]["ths_member"], {"member": "two"})

