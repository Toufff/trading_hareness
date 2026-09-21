from decimal import Decimal as D
import unittest

from app.daily_bar_repository import daily_amount_unit_mismatch
from app.daily_asset_semantics import close_conflicts, daily_asset_kind


class AssetSemanticsTest(unittest.TestCase):
    def test_exchange_disambiguates_bank_and_index(self):
        self.assertEqual(daily_asset_kind('000001.SH'), 'index')
        self.assertEqual(daily_asset_kind('000001.SZ'), 'stock')
        self.assertEqual(daily_asset_kind('512000.SH'), 'fund')

    def test_real_index_amount_is_not_index_points_times_volume(self):
        self.assertFalse(daily_amount_unit_mismatch(symbol='399001.SZ', source='eastmoney_free',
            amount=D('1084694363.40319'), volume=D('631708071'), close=D('13730.02')))

    def test_index_still_checks_contract_and_extreme_scale(self):
        for source, amount in [('unregistered', D('1084694363')), ('eastmoney_free', D('1084694363403'))]:
            self.assertTrue(daily_amount_unit_mismatch(symbol='399001.SZ', source=source,
                amount=amount, volume=D('631708071'), close=D('13730.02')))

    def test_stock_and_fund_still_reject_yuan_as_thousand_yuan(self):
        for symbol in ['000001.SZ', '512000.SH']:
            self.assertTrue(daily_amount_unit_mismatch(symbol=symbol, source='eastmoney_free',
                amount=D('1000000'), volume=D('100'), close=D('100')))

    def test_index_rounding_is_not_a_price_conflict(self):
        self.assertFalse(close_conflicts('000001.SH', D('3955.234'), D('3955.23')))
        self.assertTrue(close_conflicts('000001.SH', D('3955.25'), D('3955.23')))
        self.assertTrue(close_conflicts('512000.SH', D('1.003'), D('1.001')))
