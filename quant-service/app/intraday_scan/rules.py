"""Pure lane-specific discovery and forward-only plan evaluation.

No partial day is fed into the close scanner. Historical feature definitions are
reused; current confirmation and fixed ordering are separate from close scores.
"""
from dataclasses import asdict
from datetime import datetime,timedelta
from collections import defaultdict
from statistics import median
import hashlib,json
from . import VERSION
from ..short_term_lanes.rules import features,mainboard,Settings,LANES
from ..short_term_lanes.accumulation_rules import evaluate as accumulation

STATE_ORDER={'confirmed_observation':0,'wait_confirmation':1,'execution_uncertain':2,'invalidated':3,'data_gap':4}
LABELS=dict((k,label) for k,label,_ in LANES)
POLICY=dict(confirmation_minutes=3,minimum_amount=2e8,minimum_turnover=1.5,
            top_per_lane=5,minute_budget=60,minimum_market_symbols=3500,
            order=['state','relative_to_sector_desc','distance_from_session_high_asc','amount_desc','symbol'],
            same_time_amount='unknown unless separately observed',live_effect='none')

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()

def validate_minute_health(minutes,errors):
    if not any(minutes.values()):raise ValueError('No usable minute evidence; not an empty successful scan')
    return 'partial' if errors else 'completed'

def minute_time(day,t):
    return datetime.fromisoformat(day+'T'+t[:2]+':'+t[2:]+':00+08:00')

def consecutive(rows):
    return len(rows)==POLICY['confirmation_minutes'] and all(
        minute_time('2000-01-01',b['time'])-minute_time('2000-01-01',a['time'])==timedelta(minutes=1)
        for a,b in zip(rows,rows[1:]))

def evaluate(item,quote,minutes,cutoff,sector_change,*,previous_plan=None,plan_available_at=None):
    now=datetime.fromisoformat(cutoff);day=cutoff[:10];hhmm=now.strftime('%H%M')
    result={**item,'buy_authorized':False,'same_time_amount_status':'unavailable',
            'state':'data_gap','reason':'当期行情或分钟证据缺失','plan':previous_plan}
    if not quote:return result
    price=quote['close'];reference=item.get('reference');support=item.get('support')
    bars=sorted([m for m in minutes if '0930'<=m['time']<=hhmm],key=lambda m:m['time'])
    # Deduplicate timestamps deterministically, never count repeated ticks as minutes.
    bars=list({m['time']:m for m in bars}.values())
    result.update(price=price,change_pct=quote['pct_chg'],amount=quote['amount'],
                  turnover=quote.get('turnover_rate'),main_net=quote.get('main_net'),
                  volume_ratio=quote.get('volume_ratio'),relative_to_sector=quote['pct_chg']-sector_change,
                  sector_key=quote.get('plate_id'),sector_change=sector_change)
    if not bars or bars[-1]['time']!=hhmm:return result
    if abs(bars[-1]['close']/price-1)>.005:
        result['reason']='截面价格与最后分钟不一致，拒绝混合时间';return result
    vwap=bars[-1].get('vwap');tail=bars[-POLICY['confirmation_minutes']:]
    high=max(m['close'] for m in bars);result.update(vwap=vwap,minute_end=hhmm,
        distance_from_high_pct=(high-price)/high*100,
        vwap_gap_pct=(price/vwap-1)*100 if vwap else None)
    strong=reference and vwap and consecutive(tail) and all(m['close']>=reference and m.get('vwap') and m['close']>=m['vwap'] for m in tail)
    weak=support and consecutive(tail) and all(m['close']<support for m in tail)
    if weak:state,why='invalidated','连续分钟收盘跌破原结构参考，保留为失败/转弱样本'
    elif quote['pct_chg']>=9.7:state,why='execution_uncertain','接近涨停；不能证明排队可成交，不算买入成功'
    elif strong and sector_change>=0:state,why='confirmed_observation','连续分钟站在本策略参考和VWAP上方，板块中位表现非负；只是结构确认'
    else:state,why='wait_confirmation','本策略承接或板块条件尚未同时满足；不是从候选中删除'
    result.update(state=state,reason=why)
    plan=previous_plan if previous_plan and previous_plan.get('expires_on')==day else None
    result['previous_plan_state']='expired' if previous_plan and not plan else None
    if plan:
        plan=dict(plan)
        if plan.get('state')=='armed':
            future=[m for m in bars if minute_time(day,m['time'])>datetime.fromisoformat(plan['created_at'])]
            for i in range(2,len(future)):
                window=future[i-2:i+1]
                if not consecutive(window):continue
                if plan.get('support') and all(m['close']<plan['support'] for m in window):
                    plan.update(state='invalidated',invalidated_at=minute_time(day,window[-1]['time']).isoformat());break
                # Volume/book/board history is not available at every historical
                # minute: this is only a price trigger, NEVER a verified fill.
                platform = plan.get('setup_kind') == 'platform_hold'
                price_trigger = (plan.get('support') and plan.get('reference') and all(
                    plan['support'] <= m['close'] <= plan['reference'] and m.get('vwap') and m['close'] >= m['vwap'] for m in window)) if platform else (
                    plan.get('reference') and all(m.get('vwap') and m['close']>=max(plan['reference'],m['vwap']) for m in window))
                if price_trigger:
                    plan.update(state='triggered_unverified_fill',triggered_at=minute_time(day,window[-1]['time']).isoformat(),
                                trigger_price=window[-1]['close']);break
    elif reference and support and state not in {'invalidated','data_gap','execution_uncertain'}:
        plan=dict(reference=reference,support=support,created_at=plan_available_at or cutoff,expires_on=day,
                  state='armed',triggered_at=None,condition='未来连续3分钟收盘守在参考和VWAP上方；价格触发不证明量能、板块或成交',
                  t_plus_one=True,fill_verified=False)
    result['plan']=plan
    if previous_plan and previous_plan.get('trigger_price'):
        result['price_trigger_followup']=dict(trigger_price=previous_plan['trigger_price'],
            observed_return_pct=(price/previous_plan['trigger_price']-1)*100,
            basis='价格触发后的观察收益，未计手续费、未证实成交，不是实盘或可实现收益')
    return result

def discover(history,sessions,rows):
    grouped=defaultdict(list)
    for r in history:grouped[r['symbol']].append(r)
    out=[];settings=Settings()
    for r in rows:
        if not mainboard(r) or r['amount']<POLICY['minimum_amount'] or (r.get('turnover_rate') or 0)<POLICY['minimum_turnover']:continue
        bars=grouped[r['symbol']];f=features(bars,sessions)
        if not f:continue
        historical=sorted(bars,key=lambda b:str(b['trade_date']))
        high=max(b['close'] for b in historical[-5:]);low=min(b['close'] for b in historical[-5:])
        a=accumulation(historical);price=r['close'];pct=r['pct_chg'];ratio=r.get('volume_ratio')
        # Lane definitions are preserved. Volume ratio is explicitly a vendor
        # proxy, not a fabricated historical same-time amount ratio.
        matches={
            'accumulation':a['matched_intersection'],
            'expansion':price>high and pct>=2 and ratio is not None and ratio>=settings.expansion_multiple,
            'trend':f['return_10d']>=5 and f['ma5']>f['ma10'] and price>=f['ma5'],
            'pullback':f['prior_gain']>=8 and high*.92<=price<high and price>=f['ma10']*.98,
        }
        for lane,yes in matches.items():
            if not yes:continue
            ref=f['ma5'] if lane in {'trend','pullback'} else high
            support=f['ma10'] if lane in {'trend','pullback'} else low
            out.append(dict(symbol=r['symbol'],name=r['name'],lane=lane,source='new_intraday',
                reference=ref,support=min(support,ref),origin_id=digest([VERSION,sessions[-1],r['symbol'],lane]),
                original_reason=LABELS[lane]+'盘中适配；历史结构截至'+sessions[-1],
                discovery_evidence=dict(flow=a['flow']['score'],sideways=a['sideways']['score'],
                    return10=f['return_10d'],vendor_volume_ratio=ratio),
                caveat='回踩缩量未核验' if lane=='pullback' else '启动量能为供应商量比代理' if lane=='expansion' else '不是买入权限'))
    return out

def prepare(data):
    cutoff=data['cutoff'];day=cutoff[:10]
    if not data['sessions'] or max(data['sessions'])>=day:raise ValueError('history must be settled before intraday date')
    if any(str(r['trade_date']).replace('-','')>=day.replace('-','') for r in data['history']):raise ValueError('history must be settled')
    if any(str(r['trade_date']).replace('-','')!=day.replace('-','') for r in data['rows']):raise ValueError('wrong live date')
    if len(data['rows'])<POLICY['minimum_market_symbols'] or data['health'].get('plate_coverage',0)<.95:
        raise ValueError('market coverage insufficient')
    if data['health'].get('duplicate_conflicts'):raise ValueError('conflicting market snapshot rows')
    by={(r['symbol'],r['lane']):r for r in discover(data['history'],data['sessions'],data['rows'])}
    for seed in data['seeds']:
        if seed.get('available_at') and datetime.fromisoformat(seed['available_at'])>datetime.fromisoformat(cutoff):continue
        k=(seed['symbol'],seed['lane']);old=by.get(k)
        by[k]={**seed,'also_discovered':bool(old),'source':'previous'}
    return sorted(by.values(),key=lambda r:(r['source']!='previous',r.get('display_rank') or 999,r['symbol'],r['lane']))

def build(data):
    candidates=prepare(data);rows={r['symbol']:r for r in data['rows']};boards=defaultdict(list)
    for r in rows.values():boards[r['plate_id']].append(r['pct_chg'])
    sectors={k:median(v) for k,v in boards.items()}
    result=[]
    for item in candidates:
        q=rows.get(item['symbol']);prior=(data.get('previous_plans') or {}).get(item['origin_id'])
        result.append(evaluate(item,q,data['minutes'].get(item['symbol'],[]),data['cutoff'],
                               sectors.get(q['plate_id'],0) if q else 0,previous_plan=prior,
                               plan_available_at=data.get('observed_at',data['cutoff'])))
    lanes=[]
    def order(r):return (STATE_ORDER[r['state']],-r.get('relative_to_sector',-999),r.get('distance_from_high_pct',999),-r.get('amount',0),r['symbol'])
    for key,label,_ in LANES:
        items=sorted([r for r in result if r['lane']==key],key=order)
        lanes.append(dict(key=key,label=label,items=items,top=items[:5],
            discovery_scope='historical candidates only; no invented fresh advanced/event qualification' if key in {'contraction','rotation','reclaim','event','relay'} else 'versioned intraday adaptation'))
    previous=[r for r in result if r['source']=='previous']
    changes=[r['pct_chg'] for r in rows.values()]
    return dict(version=VERSION,policy=POLICY,cutoff=data['cutoff'],input_hash=digest(data),
        confirmation_status='partial' if any(r['state']=='data_gap' for r in result) else 'complete',
        history_through=data['sessions'][-1],market=dict(symbols=len(rows),up=sum(p>0 for p in changes),down=sum(p<0 for p in changes),median=median(changes)),
        previous=previous,lanes=lanes,health=data['health'],live_effect='none',
        previous_count=len(previous),missing_minutes=sum(r['state']=='data_gap' for r in result),
        note='分策略内固定排序；不以横盘分比较修复与趋势。不自动交易、不验证盈利。')
