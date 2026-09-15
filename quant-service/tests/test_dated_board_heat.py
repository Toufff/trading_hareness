from datetime import datetime
import unittest
from app.longhu_board_close import aggregate_board
from app.sector_heat.repository import payload, timestamp
from app.sector_heat.context import _cohorts, _comparison


class DatedBoardHeatTests(unittest.TestCase):
    def test_rebuild_never_reuses_live_catalog_values(self):
        result=aggregate_board({'sector_key':'x','label':'test','change_pct':99,'amount':999999},[
            {'symbol':'600664.SH','pct_chg':2,'main_net':10,'amount':100,'volume_ratio':2},
            {'symbol':'000001.SZ','pct_chg':-4,'main_net':-5,'amount':200,'volume_ratio':1}], '2026-09-09')
        self.assertEqual(result['change_pct'],-1)
        self.assertEqual(result['amount'],300)
        self.assertEqual(result['net_inflow'],5)
        self.assertEqual(result['advancing_breadth'],.5)
        self.assertEqual(result['volume_ratio'],1.5)
        self.assertEqual(result['trade_date'],'2026-09-09')

    def test_basis_change_cannot_create_rank_change(self):
        self.assertEqual(_comparison({'series_basis':'new'},{'series_basis':'old'},{}, {})[:2],(None,None))

    def test_timestamp_and_payload_accept_native_types(self):
        self.assertEqual(payload({'x':1}),{'x':1})
        self.assertEqual(timestamp('2026-09-09T07:00:00Z').hour,15)
