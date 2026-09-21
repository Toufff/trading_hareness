"""Asset-specific daily field contracts, independent of provider I/O.

An index level is NOT the average price of its constituents. Multiplying it
by aggregate turnover volume cannot validate an index's amount unit.
Amounts here have already been converted by adapters to thousand CNY.
"""
from decimal import Decimal
import re

INDEX_AMOUNT_PROVIDERS = frozenset({
    'tushare', 'tushare_primary', 'tushare_super_get', 'tushare_super_sdk',
    'tushare_super', 'tushare_backup', 'eastmoney_free', 'longhuvip_index',
    'longhuvip', 'longhuvip_composite', 'tencent_index_free',
})
# Sanity ceiling, NOT a currency detector. Adapter/source evidence must still
# establish the unit; crossing this bound quarantines rather than rescales.
MAX_INDEX_AMOUNT_THOUSAND_CNY = Decimal('50000000000')


def daily_asset_kind(symbol: str | None) -> str:
    value = str(symbol or '').upper()
    if re.fullmatch(r'(000\d{3}\.SH|399\d{3}\.SZ)', value):
        return 'index'
    if re.fullmatch(r'(5\d{5}\.SH|1[568]\d{4}\.SZ)', value):
        return 'fund'
    return 'stock'


def close_conflicts(symbol: str, existing: Decimal, incoming: Decimal) -> bool:
    # Some index providers round to 0.01 points; do not treat < half a last
    # decimal as a conflict. Fund 0.001 and stock 0.01 ticks stay unchanged.
    tolerance = Decimal('0.005') if daily_asset_kind(symbol) == 'index' else Decimal('0.001')
    return abs(existing - incoming) > tolerance
