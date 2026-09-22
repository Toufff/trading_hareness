"""Descriptive, versioned price/flow states. No trade decisions or fitted score.

All windows use exchange timestamps, one continuous session and bounded gaps.
Five-level (unweighted) order imbalance is auxiliary only, never a state vote.
"""
from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
from statistics import median
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .rules import AdvisorySignal, QuoteSample

VERSION = 'pressure-v1'
STATE_TEXT = {'buy_confirmed':'偏多，买方进攻获价格确认',
              'sell_confirmed':'偏空，卖方压力获价格确认',
              'buy_stalled':'买方进攻受阻，价格未跟随',
              'sell_absorbed':'卖压下价格暂稳，出现承接迹象',
              'divergent':'成交方向与价格背离，方向待确认',
              'balanced':'多空暂均衡', 'insufficient':'成交方向证据不足'}


def _session(at):
    local = at.astimezone(ZoneInfo('Asia/Shanghai'))
    minute = local.hour*60 + local.minute
    part = 'am' if 570 <= minute < 690 else 'pm' if 780 <= minute < 897 else None
    return (local.date(), part) if part else None


def _window(rows: Sequence[QuoteSample], seconds: int):
    if not rows:
        return []
    end = rows[-1]
    target = end.observed_at-timedelta(seconds=seconds)
    start = next((x for x in reversed(rows) if x.observed_at <= target), None)
    if start is None or (target-start.observed_at).total_seconds() > 15:
        return []
    segment = [x for x in rows if start.observed_at <= x.observed_at <= end.observed_at]
    if not _session(end.observed_at) or any(_session(x.observed_at) != _session(end.observed_at) or
                                           x.symbol != end.symbol for x in segment):
        return []
    if any(not 0 < (b.observed_at-a.observed_at).total_seconds() <= 20 or
           b.amount < a.amount or b.volume_lot < a.volume_lot for a,b in zip(segment,segment[1:])):
        return []
    return segment


def _rate(segment):
    return (segment[-1].amount-segment[0].amount)*60/(segment[-1].observed_at-segment[0].observed_at).total_seconds()


def window_features(rows: Sequence[QuoteSample], seconds: int) -> dict[str, Any]:
    segment = _window(rows, seconds)
    if not segment:
        return {'status':'insufficient_window', 'window_seconds':seconds, 'feature_version':VERSION}
    first, last = segment[0], segment[-1]
    amount, volume = last.amount-first.amount, last.volume_lot-first.volume_lot
    change = (last.price/first.price-1)*100
    baseline = []
    # Non-overlapping prior equal-duration windows, normalized by actual seconds.
    remaining = [x for x in rows if x.observed_at <= first.observed_at]
    for _ in range(7):
        prior = _window(remaining, seconds)
        if not prior:
            break
        baseline.append(_rate(prior))
        remaining = [x for x in remaining if x.observed_at <= prior[0].observed_at]
    normal = median(baseline) if len(baseline) >= 3 else None
    ratio = _rate(segment)/normal if normal and normal > 0 else None
    flow_valid = all(x.outer_lot is not None and x.inner_lot is not None for x in segment)
    if flow_valid:
        flow_valid = all(b.outer_lot >= a.outer_lot and b.inner_lot >= a.inner_lot
                         for a,b in zip(segment,segment[1:]))
    outer = last.outer_lot-first.outer_lot if flow_valid else None
    inner = last.inner_lot-first.inner_lot if flow_valid else None
    # Provider classification must reconcile approximately with total traded lots.
    flow_valid = bool(flow_valid and volume > 0 and .8 <= (outer+inner)/volume <= 1.2)
    active = (outer-inner)/(outer+inner) if flow_valid and outer+inner > 0 else None
    if active is None:
        state = 'insufficient'
    elif active >= .25:
        state = 'buy_confirmed' if change >= .15 else 'buy_stalled' if change > -.15 else 'divergent'
    elif active <= -.25:
        state = 'sell_confirmed' if change <= -.15 else 'sell_absorbed' if change < .15 else 'divergent'
    else:
        state = 'balanced'
    pairs = [(x, y) for x,y in zip(segment,segment[1:]) if x.bid_depth is not None and
             x.ask_depth is not None and x.bid_depth > 0 and x.ask_depth > 0]
    weights = [(x,(y.observed_at-x.observed_at).total_seconds()) for x,y in pairs]
    covered = sum(w for _,w in weights)
    elapsed = (last.observed_at-first.observed_at).total_seconds()
    book_ready = covered/elapsed >= .8 and last.bid_depth is not None and last.ask_depth is not None and last.bid_depth > 0 and last.ask_depth > 0
    book = {'status':'ready' if book_ready else 'incomplete', 'used_in_pressure':False}
    if book_ready:
        book.update(weibi_pct=round(100*(last.bid_depth-last.ask_depth)/(last.bid_depth+last.ask_depth),2),
                    mean_weibi_pct=round(sum(100*(x.bid_depth-x.ask_depth)/(x.bid_depth+x.ask_depth)*w for x,w in weights)/covered,2),
                    bid_depth_lot=last.bid_depth, ask_depth_lot=last.ask_depth,
                    coverage=round(covered/elapsed,3))
    return {'status':'ready','feature_version':VERSION, 'window_seconds':seconds,
            'actual_seconds':elapsed, 'start_at':first.observed_at.isoformat(), 'end_at':last.observed_at.isoformat(),
            'price':last.price, 'pre_close_change_pct':round((last.price/last.pre_close-1)*100,3),
            'price_change_pct':round(change,3), 'amount_delta':round(amount,2),
            'amount_per_minute':round(_rate(segment),2),
            'amount_ratio':round(ratio,3) if ratio is not None else None,
            'baseline_windows':len(baseline), 'baseline_kind':'prior_equal_window_median',
            'volume_state':'unknown' if ratio is None else 'expanded' if ratio>=1.5 else 'contracted' if ratio<=2/3 else 'normal',
            'active_ratio':round(active,4) if active is not None else None,
            'flow_coverage':round((outer+inner)/volume,3) if flow_valid else None,
            'interval_vwap':round(amount/(volume*100),4) if volume>0 and amount>0 else None,
            'price_impact_bps_per_10m':round(change*100/(amount/10000000),3) if amount>=1000000 else None,
            'pressure_state':state,'pressure_text':STATE_TEXT[state], 'book':book}


def feature_bundle(rows):
    return {str(seconds):window_features(rows,seconds) for seconds in (60,180,300)}


def signature(feature, severity):
    direction = 'up' if feature['price_change_pct'] >= .15 else 'down' if feature['price_change_pct'] <= -.15 else 'flat'
    return '|'.join((feature['pressure_state'],feature['volume_state'],direction,severity))


def pressure_event(rows, previous: dict[str,Any] | None) -> AdvisorySignal | None:
    windows = feature_bundle(rows)
    feature = windows['60']
    if feature.get('status') != 'ready':
        return None
    price_windows = [x for x,threshold in ((windows['60'],1.2),(windows['180'],2),(windows['300'],3))
                     if x.get('status') == 'ready' and abs(x['price_change_pct']) >= threshold]
    price_alarm = bool(price_windows)
    ratio = feature['amount_ratio'] or 0
    material = price_alarm or (feature['amount_delta'] >= 5000000 and ratio >= 3) or (
        ratio >= 1.5 and feature['pressure_state'] in {'buy_confirmed','sell_confirmed'})
    severity = 'high' if abs(feature['price_change_pct']) >= 1.8 or ratio >= 5 else 'medium'
    key = signature(feature,severity)
    prior = (previous or {}).get('metrics') or {}
    if key == prior.get('state_signature') or (not prior and not material):
        return None
    if prior:
        prior_at = prior.get('end_at')
        from datetime import datetime
        elapsed = (rows[-1].observed_at-datetime.fromisoformat(prior_at)).total_seconds() if prior_at else 0
        reversal = {prior.get('pressure_state'),feature['pressure_state']} == {'buy_confirmed','sell_confirmed'}
        upgrade = severity=='high' and not str(prior.get('state_signature','')).endswith('|high')
        recovery = (prior.get('pressure_state') in {'buy_confirmed','sell_confirmed'} and
                    feature['pressure_state']=='balanced' and abs(feature['price_change_pct'])<.15 and
                    feature['volume_state'] in {'contracted','normal'} and elapsed>=120)
        # Missing evidence is an operational quality event, not a market reversal.
        if not material and not recovery:
            return None
        if elapsed<600 and not (reversal or upgrade or recovery):
            return None
        # A transition must persist for 30 seconds; a high severity price burst bypasses.
        if not (upgrade and abs(feature['price_change_pct'])>=1.8):
            for offset in (15,30):
                earlier = [x for x in rows if x.observed_at <= rows[-1].observed_at-timedelta(seconds=offset)]
                prior_window = window_features(earlier,60)
                if prior_window.get('status') != 'ready' or signature(prior_window,severity) != key:
                    return None
    direction = 'up' if feature['price_change_pct'] > 0 else 'down' if feature['price_change_pct'] < 0 else 'flat'
    volume_text = {'expanded':'放量','contracted':'缩量','normal':'量能平稳','unknown':'量能基准不足'}[feature['volume_state']]
    price_text = '上涨' if direction=='up' else '下跌' if direction=='down' else '持平'
    summary = f"近1分钟{price_text} {abs(feature['price_change_pct']):.2f}% · {volume_text} · {feature['pressure_text']}"
    trigger_window = price_windows[0] if price_windows else feature
    if trigger_window['window_seconds'] != 60:
        longer_change = trigger_window['price_change_pct']
        summary = f"近{trigger_window['window_seconds']//60}分钟{'上涨' if longer_change>0 else '下跌'} {abs(longer_change):.2f}%；" + summary
    metrics = {**feature, 'windows':windows, 'state_signature':key,
               'trigger_window_seconds':trigger_window['window_seconds'],
               'previous_pressure_text':prior.get('pressure_text'),
               'previous_volume_state':prior.get('volume_state')}
    current = rows[-1]
    event_key = sha256(f'{VERSION}|{current.symbol}|{current.observed_at.isoformat()}|{key}'.encode()).hexdigest()
    return AdvisorySignal(event_key,current.symbol,current.name,'pressure_change',direction,severity,
                          current.observed_at,metrics,summary)
