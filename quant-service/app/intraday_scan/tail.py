"""Late-session preview using the formal nine-lane engine, never daily writes.

Incomplete cumulative amount is a lower bound, not proof of contraction.
No daily OHLC is manufactured from minute close samples.
"""
from copy import deepcopy
from datetime import datetime
from .rules import digest
from ..short_term_lanes.rules import screen, Settings

VERSION = 'tail-formal-preview-20260914-1'

def implementation_hash():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    paths=[Path(__file__)]+list((root/'short_term_lanes').glob('*.py'))+list((root/'ranking_factors').glob('*.py'))+[root/'short_term_liquidity.py']
    return digest({str(p.relative_to(root)):p.read_text(encoding='utf-8') for p in sorted(paths)})

def restore_settings(raw):
    from ..short_term_lanes.price_volume import PriceVolumeSettings
    from ..ranking_factors import FactorSpec
    raw=deepcopy(raw)
    raw['price_volume']=PriceVolumeSettings(**raw['price_volume'])
    raw['ranking_factors']=tuple(FactorSpec(**dict(r,strategies=tuple(r['strategies']))) for r in raw['ranking_factors'])
    return Settings(**raw)

def build(data, *, settings=None, events=None):
    cutoff=datetime.fromisoformat(data['cutoff'])
    if not '14:30' <= cutoff.strftime('%H:%M') < '15:00':
        raise ValueError('Tail preview requires a 14:30-14:59 snapshot')
    day=str(cutoff.date())
    if not data['sessions'] or max(data['sessions'])>=day:
        raise ValueError('History must precede the provisional session')
    history=deepcopy(data['history'])
    if any(str(r['trade_date']).replace('-','')>=day.replace('-','') for r in history):
        raise ValueError('Settled history contains current/future rows')
    live=deepcopy(data['rows'])
    if len({r['symbol'] for r in live})!=len(live):
        raise ValueError('Duplicate live symbols')
    if any(str(r['trade_date']).replace('-','')!=day.replace('-','') for r in live):
        raise ValueError('Wrong live date')
    if data.get('health',{}).get('plate_coverage',0)<.95:
        raise ValueError('Insufficient live plate coverage')
    sessions=data['sessions'][-10:]+[day]
    result=screen(history+live,sessions,day,events=events or {},settings=settings or Settings(),
                  information_cutoff=data['cutoff'])
    if result['status']!='completed':raise ValueError('Formal preview history coverage incomplete')
    result.update(tail_version=VERSION,phase='tail_provisional',settled=False,
        implementation_hash=implementation_hash(),
        source_input_hash=digest(data),cutoff=data['cutoff'],observed_at=data['observed_at'],
        daily_writes=False,live_effect='none',
        amount_basis='今日截至cutoff累计额/此前完整日均额：下界，不是同刻量比，不证明缩量',
        scope='九策略正式核心均执行；当日严格OHLC缺失时高级形态不得确认为通过',
        close_reconciliation='本轮冻结为尾盘暂定；收盘必须另跑正式结果，不能覆盖本轮')
    for lane in result['lanes']:
        for key in ('tracking_candidates','observation_list','selected','caution_list'):
            for item in lane.get(key,[]):
                item['tail_provisional']=True
                item['buy_authorized']=False
                item['amount_final']=False
                item['tail_execution']='需按本策略审核尾盘入场；不统一要求先突破'
                if lane['key'] in {'pullback','contraction','reclaim'}:
                    item['tail_execution']='缩量/收盘形态尚未确认，不能视为量价已通过'
                item['expiry']='仅本次尾盘快照；收盘另行复核，不是委托'
    return result

def write(result,directory):
    import json
    directory.mkdir(parents=True,exist_ok=True)
    head=f"数据截至{result['cutoff']}；今日未收盘；使用正式九策略核心、默认/已批准参数，不是新的混合排名。"
    intro=['# 尾盘正式策略预扫描','',head,'',
           '当日资金和平台已纳入重算；潜伏不以突破作为统一入选条件。候选不是已通过入场审核。',
           '成交额为截至当前累计值：已超过完整历史日均额可说明放大，低于日均额不能证明最终缩量。',
           '当日严格OHLC未提供，高级形态缺口保留；不从分钟收盘价伪造最高最低价。','']
    for lane in result['lanes']:
        rows=lane.get('observation_list',[])[:5]
        section=[f"## {lane['label']}：{lane['total_matches']}个暂定匹配",'',
                 '|股票|现价|涨幅|成交额|状态|依据|','|---|---:|---:|---:|---|---|']
        for r in rows:
            m=r['metrics'];reason=r['reason'].replace('|','/')
            state={'watch':'暂定观察','crowded':'拥挤/不当低吸','wait_next_session':'接近涨停/等次日','regime_restricted':'市场风险受限','quality_warning':'量价警示','wait_recovery':'等待修复'}.get(r['state'],r['state'])
            section.append(f"|{r['name']}（{r['symbol'].split('.')[0]}）|{m['close']:.2f}|{m['change_pct']:+.2f}%|{m['amount']/1e8:.2f}亿|{state}|{reason}|")
        if not rows:section.append('无候选；数据不足与没有匹配分别见JSON中的status/data_gaps。')
        section+=['',f"策略状态：{lane['status']}；OHLC/量能缺口：{lane['data_gaps']}",'']
        (directory/(lane['key']+'.md')).write_text('\n'.join([head,'']+section),encoding='utf-8')
        intro+=section
    (directory/'overview.md').write_text('\n'.join(intro),encoding='utf-8')
    (directory/'result.json').write_text(json.dumps(result,ensure_ascii=False,default=str),encoding='utf-8')
    return str(directory/'overview.md')
