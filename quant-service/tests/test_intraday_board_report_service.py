from __future__ import annotations

import unittest
from pathlib import Path

from app.intraday_board_report_service import summary_sections


class IntradayBoardReportServiceTests(unittest.TestCase):
    def test_summary_keeps_industry_and_concept_inflow_outflow_separate(self) -> None:
        summary, sections = summary_sections([
            {"taxonomy_key": "eastmoney_industry", "label": "行业甲", "net_inflow": 3.0},
            {"taxonomy_key": "eastmoney_industry", "label": "行业乙", "net_inflow": -2.0},
            {"taxonomy_key": "eastmoney_concept", "label": "概念甲", "net_inflow": 5.0},
            {"taxonomy_key": "eastmoney_concept", "label": "概念乙", "net_inflow": -4.0},
            {"taxonomy_key": "eastmoney_concept", "label": "缺失", "net_inflow": None},
        ], lambda value: f"{value:+.1f}")
        self.assertEqual(summary["eastmoney_industry"]["inflow"][0]["label"], "行业甲")
        self.assertEqual(summary["eastmoney_concept"]["outflow"][0]["label"], "概念乙")
        self.assertEqual(sections, ["行业流入：行业甲 +3.0；行业乙 -2.0", "行业流出：行业乙 -2.0；行业甲 +3.0",
                                    "概念流入：概念甲 +5.0；概念乙 -4.0", "概念流出：概念乙 -4.0；概念甲 +5.0"])

    def test_service_has_no_main_or_http_client_dependency(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app" / "intraday_board_report_service.py").read_text()
        self.assertNotIn("from .main", source)
        self.assertNotIn("httpx", source)
        self.assertIn("Feishu is reserved for watched-stock strategy signals", source)


if __name__ == "__main__":
    unittest.main()
