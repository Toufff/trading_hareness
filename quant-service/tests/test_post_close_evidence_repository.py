from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import MagicMock

from app.post_close_evidence_repository import load_exact_board_context_rows


class PostCloseEvidenceRepositoryTests(unittest.TestCase):
    def test_exact_context_falls_back_to_saved_longhu_industry_membership(self) -> None:
        database = MagicMock()
        connection = MagicMock()
        database.transaction.return_value.__enter__.return_value = connection
        result = MagicMock()
        result.fetchall.return_value = [{
            "symbol": "600000.SH", "sector_key": "881181", "label": "银行",
            "net_amount": 100, "provider_key": "longhuvip_composite",
            "taxonomy_key": "longhu_ths_industry",
        }]
        connection.execute.return_value = result

        rows = load_exact_board_context_rows(database, date(2026, 9, 1))

        self.assertEqual(rows[0]["provider_key"], "longhuvip_composite")
        query, params = connection.execute.call_args.args
        self.assertIn("stock.raw->>'plate_id'", query)
        self.assertIn("jsonb_array_elements", query)
        self.assertEqual(params, {"trade_date": date(2026, 9, 1)})


if __name__ == "__main__":
    unittest.main()
