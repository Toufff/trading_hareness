"""Frozen nine-lane intraday adapter. Never persists settled daily prices."""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
from statistics import median
from .rules import digest, evaluate, LABELS, STATE_ORDER
from .tail import restore_settings
from ..short_term_lanes.rules import screen, Settings

VERSION = 'intraday-nine-20260914-1'


def implementation_hash():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    paths = [p for folder in ('intraday_scan', 'short_term_lanes', 'ranking_factors')
             for p in (root / folder).glob('*.py')] + [root / 'short_term_liquidity.py']
    return digest({str(p.relative_to(root)): p.read_text(encoding='utf-8') for p in sorted(paths)})


def validate(data):
    cutoff = datetime.fromisoformat(data['cutoff'])
    observed = datetime.fromisoformat(data['observed_at'])
    day = str(cutoff.date())
    if cutoff.utcoffset() != timedelta(hours=8) or not '09:30' <= cutoff.strftime('%H:%M') <= '15:00':
        raise ValueError('Invalid Shanghai trading cutoff')
    if observed < cutoff or observed.date() != cutoff.date():
        raise ValueError('Invalid observation time')
    if not data['sessions'] or max(data['sessions']) >= day:
        raise ValueError('History must precede live session')
    if any(str(r['trade_date']).replace('-', '') >= day.replace('-', '') for r in data['history']):
        raise ValueError('Future/current settled history')
    rows = data['rows']
    if len(rows) < 3500 or data['health'].get('plate_coverage', 0) < .95:
        raise ValueError('Insufficient market coverage')
    if len({r['symbol'] for r in rows}) != len(rows) or data['health'].get('duplicate_conflicts'):
        raise ValueError('Conflicting/duplicate market rows')
    if any(str(r['trade_date']).replace('-', '') != day.replace('-', '') for r in rows):
        raise ValueError('Wrong live date')
    captured = data.get('ohlc_captured_at')
    if data.get('price_histories'):
        if not captured or datetime.fromisoformat(captured) > observed:
            raise ValueError('OHLC capture provenance missing/future')
        stamp = datetime.fromisoformat(captured)
        if stamp.date() != cutoff.date() or (cutoff.hour < 15 and (stamp.hour >= 15 or stamp - cutoff > timedelta(minutes=10))):
            raise ValueError('Cannot enrich historical intraday snapshot with later daily OHLC')
        if any(r['date'] > day for bars in data['price_histories'].values() for r in bars):
            raise ValueError('Future OHLC')
    return cutoff


def formal(data):
    cutoff = validate(data)
    settings = restore_settings(data['settings']) if data.get('settings') else Settings()
    result = screen(data['history'] + data['rows'], data['sessions'][-10:] + [str(cutoff.date())],
                    str(cutoff.date()), events=data.get('events', {}), settings=settings,
                    price_histories=data.get('price_histories', {}), history_health=data.get('history_health', {}),
                    intraday=cutoff.hour < 15,
                    information_cutoff=data['cutoff'])
    if result['status'] != 'completed':
        raise ValueError('Formal strategy history coverage incomplete')
    return result


def candidates(data, scan):
    result = {}
    for lane in scan['lanes']:
        display = {r['symbol']: i + 1 for i, r in enumerate(lane.get('observation_list', []))}
        for r in lane.get('tracking_candidates', []):
            m = r['metrics']; advanced = m.get('advanced') or {}
            reference = (advanced.get('reclaim_reference') if lane['key'] == 'reclaim' else
                         m.get('ma5') if lane['key'] in {'trend', 'pullback'} else m.get('prior_high'))
            support = advanced.get('panic_low') or m.get('recent_low') or m.get('ma10')
            item = dict(symbol=r['symbol'], name=r['name'], lane=lane['key'], source='new_intraday',
                        origin_id=digest([VERSION, data['cutoff'][:10], r['symbol'], lane['key']]),
                        reference=reference, support=support, display_rank=display.get(r['symbol']),
                        original_reason=r['reason'], original_confirmation=r.get('confirmation'),
                        current_reason=r['reason'],
                        original_invalidation=r.get('invalidation'), formal_state=r['state'],
                        formal_rank=r.get('rank_score', r.get('score')), metrics=m)
            result[(r['symbol'], lane['key'])] = item
    for old in data.get('seeds', []):
        if old.get('available_at') and datetime.fromisoformat(old['available_at']) > datetime.fromisoformat(data['cutoff']):
            continue
        key = (old['symbol'], old['lane']); new = result.get(key)
        result[key] = {**(new or {}), **old, 'also_discovered': bool(new), 'source': 'previous',
                       'metrics': (new or {}).get('metrics', {}), 'formal_rank': (new or {}).get('formal_rank')}
    return list(result.values())


def build(data):
    core = formal(data); cutoff = datetime.fromisoformat(data['cutoff']); closed = cutoff.hour == 15
    quotes = {r['symbol']: r for r in data['rows']}; sectors = defaultdict(list)
    for q in quotes.values(): sectors[q['plate_id']].append(q['pct_chg'])
    sector_changes = {k: median(v) for k, v in sectors.items()}
    items = []
    for item in candidates(data, core):
        q = quotes.get(item['symbol']); bars = data.get('minutes', {}).get(item['symbol'], [])
        r = evaluate(item, q, bars, data['cutoff'], sector_changes.get(q['plate_id'], 0) if q else 0,
                     previous_plan=data.get('previous_plans', {}).get(item['origin_id']),
                     plan_available_at=data['observed_at'])
        # Latent accumulation is a platform hold, NOT necessarily a breakout.
        if item['lane'] == 'accumulation' and r.get('minute_end') and r['state'] == 'wait_confirmation':
            if item.get('support') and item.get('reference') and item['support'] <= r['price'] <= item['reference']:
                r.update(state='platform_observation', reason='仍在原平台内，潜伏观察不要求先突破；资金、承接及失效线分别检查')
        r['evidence_gaps'] = []
        r['matched_today'] = item['source'] != 'previous' or bool(item.get('also_discovered'))
        if not r['matched_today']:
            r['evidence_gaps'].append('历史候选延续跟踪；本轮没有重新满足该策略全部筛选条件')
        if not closed and item['lane'] in {'pullback', 'contraction', 'reclaim'}:
            r['evidence_gaps'].append('当日总成交额/收盘形态未完成；不能用半日低累计额证明缩量')
            if r['state'] == 'confirmed_observation': r['state'] = 'wait_confirmation'
        threshold=(data.get('settings') or {}).get('expansion_multiple' if item['lane']=='expansion' else 'pullback_multiple',1.3 if item['lane']=='expansion' else 1.05)
        if not closed and item['lane'] in {'expansion','event'} and (item.get('metrics') or {}).get('amount_multiple',0) < threshold:
            r['evidence_gaps'].append('启动量能由供应商量比代理发现；同刻成交额对照尚未取得，不能视作完整量价确认')
            if r['state'] == 'confirmed_observation': r['state'] = 'wait_confirmation'
        if item.get('formal_state') in {'crowded','quality_warning','regime_restricted','wait_recovery','wait_next_session'}:
            risk_labels={'crowded':'短期拥挤','quality_warning':'量价警示','regime_restricted':'市场环境限制','wait_recovery':'等待修复','wait_next_session':'等待下一交易日'}
            r['evidence_gaps'].append('正式策略风险状态：'+risk_labels[item['formal_state']]+'；不能被分钟价格转强覆盖')
            if r['state'] in {'confirmed_observation','platform_observation'}:r['state']='wait_confirmation'
        if r.get('minute_end') is None: r['evidence_gaps'].append('缺少与市场窗口一致的分钟末值')
        r['amount_basis'] = '完整交易时段累计额' if closed else '截至市场窗口累计额，日均比仅为下界'
        r['same_time_amount_status'] = 'not_required_after_close' if closed else 'unavailable'
        r['expiry'] = '本轮观察；不是委托；下一轮重新检查，未成交计划当日到期'
        r['entry_scenario'] = ('平台内部小幅承接，原平台低点失守则取消；突破可另作启动观察' if item['lane'] == 'accumulation'
            else item.get('original_confirmation') or '等待本策略结构和板块证据同时成立')
        if r.get('plan') and r['plan'].get('created_at') == data['observed_at']:
            r['plan']['setup_kind'] = 'platform_hold' if item['lane'] == 'accumulation' else 'reference_reclaim'
            r['plan']['condition'] = r['entry_scenario']
            if closed: r['plan']['state'] = 'after_close_watch_only'
        r['chart'] = [{k: b.get(k) for k in ('time', 'close', 'vwap', 'amount')} for b in bars if b['time'] <= cutoff.strftime('%H%M')]
        r['ohlc'] = data.get('price_histories', {}).get(item['symbol'], [])[-60:]
        r['buy_authorized'] = False
        items.append(r)
    lanes = []
    order = {**STATE_ORDER, 'platform_observation': 1}
    for lane in core['lanes']:
        current = sorted([r for r in items if r['lane'] == lane['key']], key=lambda r:
                         (not r['matched_today'], order[r['state']], -(r.get('formal_rank') or 0), -r.get('amount', 0), r['symbol']))
        lanes.append(dict(key=lane['key'], label=lane['label'], items=current, top=current[:5],
                          status='partial' if any(lane['data_gaps'].values()) else lane['status'],
                          data_gaps=lane['data_gaps'], total_matches=lane['total_matches'],
                          discovery_scope='九策略正式核心重算；盘中量价与执行证据单列，不等于收盘确认'))
    changes = [r['pct_chg'] for r in quotes.values()]
    return dict(version=VERSION, input_hash=digest(data), implementation_hash=implementation_hash(), cutoff=data['cutoff'],
                observed_at=data['observed_at'], ohlc_captured_at=data.get('ohlc_captured_at'),
                phase='after_close_initialization' if closed else 'tail' if cutoff.hour == 14 and cutoff.minute >= 30 else 'intraday',
                history_through=data['sessions'][-1], lanes=lanes, previous=[r for r in items if r['source'] == 'previous'],
                previous_count=sum(r['source'] == 'previous' for r in items), health=data['health'],
                history_health=data.get('history_health', {}), minute_health=data.get('minute_health', {}),
                governance_config=data.get('governance_config', {}),
                event_research=data.get('event_research'),
                confirmation_status='partial' if any(r['evidence_gaps'] for r in items) else 'complete',
                market=dict(symbols=len(quotes), up=sum(x > 0 for x in changes), down=sum(x < 0 for x in changes), median=median(changes)),
                market_regime=core.get('market'), live_effect='none', daily_writes=False,
                note='策略匹配不是买入授权。分钟是成交采样线，不伪造OHLC。盘中日K按真实采集时刻标记，闭市初始化不倒签尾盘建议。')
