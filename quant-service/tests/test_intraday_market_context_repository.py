from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from app.intraday_market_context_repository import (
    market_context_from_board_report,
    point_in_time_market_context_batch,
)


class IntradayMarketContextRepositoryTests(unittest.TestCase):
    def test_saved_report_context_keeps_only_matching_top_stock_evidence(self) -> None:
        observed_at = datetime(2026, 8, 17, 2, 5, tzinfo=timezone.utc)
        row = {
            "board_report_id": "board-1", "observed_at": datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc),
            "payload": {"items": [
                {"taxonomy_key": "ths_concept_flow", "sector_key": "pcb", "label": "PCB",
                 "net_inflow": 12.0, "change_pct": 1.2,
                 "top_stocks": [{"symbol": "000001.SZ"}]},
                {"taxonomy_key": "ths_concept_flow", "sector_key": "other", "label": "Other",
                 "net_inflow": 99.0, "change_pct": 2.0,
                 "top_stocks": [{"symbol": "600000.SH"}]},
            ]},
        }
        context = market_context_from_board_report(
            row, observed_at, "000001.SZ",
            strategy_market_state=lambda items: ("risk_on", {"known_board_flows": len(items)}),
            number=lambda value: float(value) if value is not None else None,
        )
        self.assertEqual(context["market_state"], "risk_on")
        self.assertEqual(context["board_snapshot_age_seconds"], 300.0)
        self.assertEqual(context["symbol_board_matches"], [{
            "taxonomy_key": "ths_concept_flow", "sector_key": "pcb", "label": "PCB",
            "net_inflow": 12.0, "change_pct": 1.2,
        }])

    def test_batch_uses_one_query_and_selects_latest_report_per_observation(self) -> None:
        connection = MagicMock()
        first = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)
        second = datetime(2026, 8, 17, 2, 10, tzinfo=timezone.utc)
        connection.execute.return_value.fetchall.return_value = [
            {"board_report_id": "first", "observed_at": first, "payload": {}},
            {"board_report_id": "second", "observed_at": second, "payload": {}},
        ]

        contexts = point_in_time_market_context_batch(
            connection,
            [(datetime(2026, 8, 17, 2, 5, tzinfo=timezone.utc), "000001.SZ"),
             (datetime(2026, 8, 17, 2, 15, tzinfo=timezone.utc), "600000.SH")],
            context_from_board_report=lambda row, _at, symbol: {"symbol": symbol, "report": row["board_report_id"] if row else None},
        )

        self.assertEqual(connection.execute.call_count, 1)
        self.assertEqual(contexts[(datetime(2026, 8, 17, 2, 5, tzinfo=timezone.utc), "000001.SZ")]["report"], "first")
        self.assertEqual(contexts[(datetime(2026, 8, 17, 2, 15, tzinfo=timezone.utc), "600000.SH")]["report"], "second")
        sql, params = connection.execute.call_args.args
        self.assertIn("SELECT max(observed_at)", sql)
        self.assertEqual(params, (datetime(2026, 8, 17, 2, 15, tzinfo=timezone.utc),
                                  datetime(2026, 8, 17, 2, 5, tzinfo=timezone.utc),
                                  datetime(2026, 8, 17, 2, 5, tzinfo=timezone.utc)))

    def test_empty_batch_does_not_query_database(self) -> None:
        connection = MagicMock()
        self.assertEqual(
            point_in_time_market_context_batch(connection, [], context_from_board_report=lambda *_: {}),
            {},
        )
        connection.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
