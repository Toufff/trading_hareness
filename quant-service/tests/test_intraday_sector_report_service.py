from __future__ import annotations

from datetime import date
from unittest import TestCase

from app.intraday_sector_report_service import build_intraday_sector_report_from_membership


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_):
        return False


class _Connection:
    def __init__(self, responses):
        self.responses = iter(responses)

    def execute(self, *_args):
        return _Result(next(self.responses))


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Db:
    def __init__(self, responses):
        self.connection = _Connection(responses)

    def transaction(self):
        return _Transaction(self.connection)


class IntradaySectorReportServiceTests(TestCase):
    def test_exact_membership_join_and_tushare_context_are_projected(self):
        db = _Db([
            [{"sector_key": "pcb", "symbol": "000001.SZ", "label": "PCB"}],
            [{"sector_key": "concept-1", "label": "概念一", "net_amount": 8, "change_pct": 2, "trading_date": date(2026, 8, 22)}],
            [{"sector_key": "concept-1", "symbol": "000001.SZ"}],
            [{"taxonomy_key": "ths_concept_flow", "latest_trade_date": date(2026, 8, 22), "rows": 1}],
            [{"api_name": "moneyflow", "latest_trade_date": "20260822", "symbols": 1, "rows": 1}],
            [{"api_name": "rt_k", "latest_available_at": "2026-08-22T01:00:00Z", "rows": 1}],
        ])
        quotes = {"000001.SZ": {"symbol": "000001.SZ", "main_net_inflow": 12, "turnover": 100}}

        def ths_top(flow_rows, member_rows, quote_rows, top_n):
            self.assertEqual(flow_rows[0]["sector_key"], "concept-1")
            self.assertEqual(member_rows[0]["symbol"], "000001.SZ")
            return ([{"taxonomy_key": "ths_concept_flow", "sector_key": "concept-1", "top_stocks": [quote_rows["000001.SZ"]]}], {"flow_boards": 1, "boards_with_members": 1, "quoted_members": 1})

        report, coverage, sector_context, stock_context, realtime_context = build_intraday_sector_report_from_membership(
            db, ("concept",), [[{"板块代码": "pcb", "板块名称": "PCB", "流入资金": 10, "流出资金": 3}]],
            quotes, 10, date(2026, 8, 22), number=lambda value: float(value) if value is not None else None,
            ths_top_stocks=ths_top,
        )
        self.assertEqual(report[0]["net_inflow"], 7.0)
        self.assertEqual(report[0]["mapped_members"], 1)
        self.assertEqual(coverage["concept"]["boards_with_members"], 1)
        self.assertEqual(coverage["ths_concept"]["quoted_members"], 1)
        self.assertEqual(stock_context[0]["api_name"], "moneyflow")
        self.assertEqual(realtime_context[0]["api_name"], "rt_k")


if __name__ == "__main__":
    import unittest
    unittest.main()
