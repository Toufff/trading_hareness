"""Immutable discovery evidence and descriptive forward observations, not trades."""
from datetime import date, datetime
import hashlib
import json
from math import isfinite

VERSION = 'observation-followup-2026-09-11'
HORIZONS = (1, 3, 5, 10)


def number(value):
    try:
        v=float(value)
        return v if isfinite(v) else None
    except (ValueError,TypeError):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def origins(result, available_at, source):
    day=result['as_of_date']
    profile=digest({'version':result['version'],'settings':result.get('settings',{}),
                    'strategy_code_hash':result.get('strategy_code_hash','legacy_unverified')})
    emitted=[]
    reviews={r['symbol']:r for r in result.get('company_reviews',[]) if r.get('symbol')}
    for lane in result['lanes']:
        selected={r['symbol']:i+1 for i,r in enumerate(lane.get('selected',[]))}
        discovery = {r['symbol']: i+1 for i,r in enumerate(sorted(
            lane.get('tracking_candidates', []), key=lambda r: (-r.get('discovery_score', r.get('rank_score', 0)), r['symbol'])))}
        observations = {r['symbol']:i+1 for i,r in enumerate(lane.get('observation_list', []))}
        candidates=lane.get('tracking_candidates',lane.get('selected',[])+lane.get('caution_list',[]))
        for rank,item in enumerate(candidates,1):
            m=item.get('metrics',{})
            item_origin=dict(signal_date=day,symbol=item['symbol'],name=item['name'],lane=lane['key'],
                lane_label=lane['label'],profile=profile,model_version=result['version'],
                rank=rank,display_rank=selected.get(item['symbol']),state=item.get('state','watch'),
                close=number(m.get('close')),confirmation=item.get('confirmation',''),
                invalidation=item.get('invalidation',''),reason=item.get('reason',''),
                reference_high=number(m.get('prior_high',m.get('prior_high5'))),reference_low=number(m.get('recent_low',m.get('low5'))),
                factor_overlay=item.get('factor_overlay'),settings=result.get('settings',{}),
                company_review=item.get('company_review') or reviews.get(item['symbol']),source=source,trade_authorized=False)
            from ..effectiveness.capture import features as effect_features
            item_origin['effectiveness'] = dict(schema=1, rank=discovery.get(item['symbol'],rank),
                display_rank=observations.get(item['symbol']), source_kind='machine',
                regime=(result.get('market',{}).get('regime') or {}).get('label','unknown'),
                features=effect_features(item,lane['key']))
            # Identity does not depend on rerun time or on future tracking outcomes.
            item_origin['origin_id']=digest(item_origin)
            timestamp=str(available_at)
            item_origin.update(available_at=timestamp,
                timing='prospective' if timestamp[:10]==day and source=='live_scan' else 'reconstructed')
            emitted.append(item_origin)
    return emitted


def evaluate(origin, sessions, bars, as_of_date):
    eligible=sorted({str(d) for d in sessions if origin['signal_date']<str(d)<=as_of_date})[:10]
    by={str(b['trading_date']):b for b in bars if str(b['trading_date']) in eligible}
    base=number(origin.get('close'))
    windows={}
    for h in HORIZONS:
        day=eligible[h-1] if len(eligible)>=h else None
        price=number(by.get(day,{}).get('close'))
        windows[str(h)]=dict(date=day,status='not_due' if day is None else 'missing' if not base or price is None else 'observed',
                            return_pct=round((price/base-1)*100,4) if base and price is not None else None)
    available=[by[d] for d in eligible if d in by]
    path=[]
    for b in available:
        high,low=number(b.get('high')),number(b.get('low'))
        rh,rl=origin.get('reference_high'),origin.get('reference_low')
        upper=bool(high is not None and rh is not None and high>=rh)
        lower=bool(low is not None and rl is not None and low<rl)
        if upper or lower:path.append(dict(date=str(b['trading_date']),upper_touched=upper,lower_broken=lower))
    if any(x['upper_touched'] and x['lower_broken'] for x in path):state='both_touched_order_unknown'
    elif any(x['lower_broken'] for x in path):state='reference_low_broken'
    elif any(x['upper_touched'] for x in path):state='reference_high_touched'
    else:state='no_reference_touch' if available else 'no_data'
    last=by.get(eligible[-1],{}) if eligible else {}
    prices=[number(b.get('close')) for b in available]
    returns=[(p/base-1)*100 for p in prices if p is not None and base]
    highs=[(float(b['high'])/base-1)*100 for b in available if number(b.get('high')) is not None and base]
    lows=[(float(b['low'])/base-1)*100 for b in available if number(b.get('low')) is not None and base]
    series=[dict(date=d,close=number(by[d].get('close')),main_net=number(by[d].get('main_net')),
        amount=number(by[d].get('amount')),pct_chg=number(by[d].get('pct_chg')),
        limit_state='closed_limit_up' if number(by[d].get('limit_up')) and abs(float(by[d]['close'])-float(by[d]['limit_up']))<.011 else
                    'closed_limit_down' if number(by[d].get('limit_down')) and abs(float(by[d]['close'])-float(by[d]['limit_down']))<.011 else 'not_confirmed') for d in eligible if d in by]
    return dict(origin_id=origin['origin_id'],symbol=origin['symbol'],name=origin['name'],lane=origin['lane'],
        signal_date=origin['signal_date'],display_rank=origin.get('display_rank'),rank=origin['rank'],
        as_of_date=as_of_date,version=VERSION,timing=origin['timing'],windows=windows,
        latest_close=number(last.get('close')),latest_return_pct=round((float(last['close'])/base-1)*100,4) if base and number(last.get('close')) is not None else None,
        observed_sessions=len(available),expected_sessions=len(eligible),missing_dates=[d for d in eligible if d not in by],
        status='pending' if not eligible else 'data_gap' if len(available)<len(eligible) else 'completed' if len(eligible)==10 else 'tracking',
        max_high_return_pct=round(max(highs),4) if highs else None,min_low_return_pct=round(min(lows),4) if lows else None,
        max_close_return_pct=round(max(returns),4) if returns else None,min_close_return_pct=round(min(returns),4) if returns else None,
        path_check=state,reference_events=path,ohlc_days=len(highs),
        series=series,reference_scope='发现时的收盘结构上下沿，仅作客观触线记录，不代替原交易条件',
        original_confirmation=origin['confirmation'],original_invalidation=origin['invalidation'],
        original_analysis=(origin.get('company_review') or {}).get('conclusion'),
        reference_high=origin.get('reference_high'),reference_low=origin.get('reference_low'),
        execution_status='not_verified',trade_return_pct=None,
        note='相对发现日收盘的观察表现，不是可成交收益；日线触线不等于量能、回踩、VWAP等完整条件成立。')
