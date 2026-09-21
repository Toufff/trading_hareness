"""Frozen daily-only B-point experiment, independent of live recommendations.

This is an explicitly named research proxy, not a translation of the original
prose entry condition. It never writes orders, discipline lines or pool ranks.
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

from ..effectiveness.execution import simulate

VERSION = 'daily-breakout-pullback-shadow-v1'
SH = ZoneInfo('Asia/Shanghai')


def freeze(origin, metrics, available_at):
    reference = origin.get('reference_high')
    support = origin.get('reference_low')
    close = origin.get('close')
    if not all(isinstance(v, (int, float)) and v > 0 for v in (reference, support, close)):
        return {'version': VERSION, 'status': 'unavailable', 'reason': 'original_structure_missing'}
    amount = metrics.get('amount_mean5') or metrics.get('avg_amount5')
    if not amount and metrics.get('amount') and metrics.get('amount_multiple'):
        amount = metrics['amount'] / metrics['amount_multiple']
    return {'version': VERSION, 'status': 'frozen', 'available_at': available_at,
            'scenario': 'pullback' if close > reference else 'breakout',
            'reference': reference, 'support': support, 'amount_baseline': amount,
            'amount_ratio_min': 1.0, 'expiry_sessions': 10, 'holding_sessions': 5,
            'entry': 'confirmed_close_then_next_session_open',
            'sector_condition': 'not_modeled_in_this_daily_proxy',
            'live_effect': 'none', 'production_buy_signal': False,
            'method': '独立日线影子实验：平台突破/回踩收复且成交额恢复；次日开盘代理成交。不是原文字条件完整确认。'}


def evaluate(origin, sessions, raw_bars, adjusted_bars, as_of_date):
    contract = origin.get('virtual_entry_contract') or {}
    events = [{'kind': 'discovered', 'date': origin['signal_date']}]
    def out(state, **kwargs):
        return dict(state=state, contract=contract, events=events, live_effect='none',
                    actual_trade=False, **kwargs)
    if contract.get('status') != 'frozen':
        return out('unregistered', reason='历史候选未事前冻结这个实验，不补造买点或收益')
    eligible = sorted({str(d) for d in sessions if origin['signal_date'] < str(d) <= as_of_date})[:contract['expiry_sessions']]
    by = {str(b['trading_date']): b for b in adjusted_bars}
    raw = {str(b['trading_date']): b for b in raw_bars}
    available = datetime.fromisoformat(contract['available_at']).astimezone(SH)
    signal = None
    for day in eligible:
        # A missing intervening session can hide a trigger/cancellation. Do not
        # skip it and silently choose a later, more profitable entry.
        b = by.get(day)
        if not b or any(b.get(k) is None for k in ('close', 'low', 'amount')):
            return out('data_gap', missing_date=day)
        if available >= datetime.combine(datetime.fromisoformat(day).date(), time(15), SH):
            continue
        if b['close'] < contract['support']:
            events.append({'kind': 'invalidated', 'date': day})
            return out('invalidated')
        if not contract.get('amount_baseline'):
            return out('data_gap', reason='frozen_volume_baseline_missing')
        recovered = b['close'] > contract['reference']
        touched = b['low'] <= contract['reference']
        if recovered and (contract['scenario'] == 'breakout' or touched) and b['amount'] >= contract['amount_baseline'] * contract['amount_ratio_min']:
            signal = day
            events.append({'kind': 'conditions_met', 'date': day, 'basis': 'settled_daily'})
            break
    if signal is None:
        return out('expired' if len(eligible) >= contract['expiry_sessions'] else 'waiting')
    future = sorted({str(d) for d in sessions if signal < str(d) <= as_of_date})[:contract['holding_sessions']]
    if not future:
        return out('awaiting_next_session', signal_date=signal)
    # Use the exact shared lot-size, costs, T+1, suspension, limit and corporate
    # action gates. A one-day record is NOT a completed hypothetical trade.
    if len(future) < contract['holding_sessions']:
        return out('execution_window_pending', signal_date=signal, observed_sessions=len(future))
    fields = {'open','high','low','close','pre_close','limit_up','limit_down','adj_factor'}
    execution = simulate([{**{k:float(v) if k in fields and v is not None else v for k,v in raw[d].items()},
                          'date': d} for d in future if d in raw], future)
    if execution['status'] == 'simulated':
        events.extend([{'kind': 'virtual_fill', 'date': future[0]}, {'kind': 'virtual_exit', 'date': future[-1]}])
    else:
        events.append({'kind': 'execution_not_verified', 'date': future[0], 'reason': execution['status']})
    return out('simulated' if execution['status'] == 'simulated' else 'execution_blocked',
               signal_date=signal, execution=execution)
