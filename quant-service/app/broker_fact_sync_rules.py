"""Strict broker facts and scheduled access rules; no UI or database control."""
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def number(value, label):
    if value is None or isinstance(value, bool):
        raise ValueError(f'BROKER_INCOMPLETE_ACCOUNT: {label}')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f'BROKER_INVALID_VALUE: {label}')
    return result


def validate_exact_totals(account, positions):
    for key in ('total_assets', 'market_value', 'available_cash'):
        number(account.get(key), key)
    expected = number(account['market_value'], 'market_value')
    total = Decimal(0)
    for p in positions:
        qty = number(p.get('quantity'), 'quantity')
        available = number(p.get('available_quantity'), 'available_quantity')
        price = number(p.get('price'), 'price')
        value = number(p.get('market_value'), 'position_market_value')
        if qty <= 0 or available > qty:
            raise ValueError('BROKER_INVALID_QUANTITY')
        if abs(qty * price - value) > max(Decimal(1), value * Decimal('.001')):
            raise ValueError('BROKER_POSITION_VALUE_MISMATCH')
        total += value
    if abs(total - expected) > max(Decimal(1), expected * Decimal('.001')):
        raise ValueError('BROKER_INCOMPLETE_POSITIONS: market values do not reconcile')
    # Frozen cash, funds and other assets mean total_asset != stock_value + available_cash.


def window_status(now, phase, calendar_open):
    local = now.astimezone(SHANGHAI)
    if phase == 'manual': return 'allowed'
    if calendar_open is None:
        raise ValueError('BROKER_TRADING_CALENDAR_MISSING')
    if not calendar_open: return 'closed'
    windows = {'midday': (time(12), time(12, 30)), 'close': (time(15, 15), time(15, 35))}
    start, end = windows[phase]
    if not start <= local.time().replace(tzinfo=None) <= end:
        raise ValueError('BROKER_ACCESS_WINDOW_EXPIRED')
    return 'allowed'


def validate_quote(quote, displayed_price, observed_at, phase):
    raw = str(quote.get('trade_time') or '')
    try:
        event = datetime.strptime(raw, '%Y%m%d%H%M%S').replace(tzinfo=SHANGHAI)
    except ValueError as error:
        raise ValueError('BROKER_FRESHNESS_UNPROVEN: quote timestamp absent') from error
    observed = observed_at.astimezone(SHANGHAI)
    floor = time(11, 29) if phase == 'midday' or observed.time() < time(15) else time(14, 59)
    if event.date() != observed.date() or event.time().replace(tzinfo=None) < floor:
        raise ValueError('BROKER_FRESHNESS_UNPROVEN: quote is not current session')
    if abs(number(quote.get('price'), 'quote_price') - number(displayed_price, 'displayed_price')) > Decimal('.011'):
        raise ValueError('STALE_BROKER_VIEW: price disagrees with independent Longhu quote')
