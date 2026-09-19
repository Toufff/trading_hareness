"""Bounded ledger reader and deduplicated feedback publication, no live weights."""
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from .rules import evaluate, Policy, VERSION, cohort, clean
from .experiments import shadow_compare
from .execution import simulate, Costs


def load_rows(database, day, horizon=5):
    with database.transaction() as c:
        records=c.execute('''SELECT o.evidence AS origin, e.evidence AS outcome
            FROM quant.strategy_observation_origins o
            LEFT JOIN LATERAL (SELECT evidence FROM quant.strategy_observation_evaluations
                WHERE origin_id=o.origin_id AND as_of_date<=%s ORDER BY as_of_date DESC,recorded_at DESC LIMIT 1) e ON TRUE
            WHERE o.signal_date BETWEEN %s AND %s ORDER BY o.signal_date,o.origin_id LIMIT 120001''',
            (day,day-timedelta(days=400),day)).fetchall()
    if len(records)>120000:raise ValueError('Effectiveness ledger exceeds bounded read; partition by cohort')
    rows=[]
    for record in records:
        o=record['origin'];e=record['outcome'] or {};f=o.get('effectiveness') or {}
        rows.append(dict(origin_id=o['origin_id'],symbol=o['symbol'],name=o['name'],lane=o['lane'],
            profile=o['profile'],regime=f.get('regime','legacy_unknown'),source_kind=f.get('source_kind','machine_legacy'),
            timing=o['timing'],signal_date=o['signal_date'],available_at=o['available_at'],
            rank=f.get('rank',o['rank']),display_rank=f.get('display_rank',o.get('display_rank')),
            selected=f.get('selected',bool(f.get('display_rank',o.get('display_rank')))),features=f.get('features',{}),
            window=((e.get('windows') or {}).get(str(horizon),{}) if e.get('calendar_complete') is True else {})))
    return rows


def load_execution(database, rows, horizon=5):
    if not rows:return {}
    ends=[r['window']['date'] for r in rows if r.get('window',{}).get('date')]
    if not ends:return {r['origin_id']:{'status':'not_due'} for r in rows}
    start=min(r['signal_date'] for r in rows);end=max(ends)
    symbols=sorted({r['symbol'] for r in rows})
    with database.transaction() as c:
        sessions=c.execute('''SELECT calendar_date,is_open FROM quant.market_trade_calendar
            WHERE exchange='SSE' AND calendar_date>%s AND calendar_date<=%s ORDER BY calendar_date''',(start,end)).fetchall()
        bars=c.execute('''SELECT symbol,trading_date,open,high,low,close,pre_close,limit_up,limit_down,adj_factor,is_suspended
            FROM quant.canonical_bars_daily WHERE symbol=ANY(%s) AND trading_date>%s AND trading_date<=%s
            ORDER BY trading_date LIMIT 250001''',(symbols,start,end)).fetchall()
    if len(bars)>250000:raise ValueError('Execution history exceeds bounded read')
    by=defaultdict(list)
    for b in bars:
        by[b['symbol']].append({k:(float(v) if k in ('open','high','low','close','limit_up','limit_down','adj_factor') and v is not None else v) for k,v in b.items()}|{'date':str(b['trading_date'])})
    known={str(r['calendar_date']) for r in sessions}
    begin=date.fromisoformat(start);finish=date.fromisoformat(end)
    if any(str(begin+timedelta(days=i)) not in known for i in range(1,(finish-begin).days+1) if (begin+timedelta(days=i)).weekday()<5):
        return {r['origin_id']:{'status':'calendar_gap'} for r in rows}
    days=[str(r['calendar_date']) for r in sessions if r['is_open']];out={}
    for r in rows:
        future=[d for d in days if d>r['signal_date']][:horizon]
        out[r['origin_id']]=simulate(by[r['symbol']],future,Costs()) if len(future)==horizon else {'status':'not_due'}
    return out


def build(database, day, *, write=True, policy=Policy()):
    from ..strategy_governance.review_evidence import archive_json,write_packet,code_excerpt,attach_review_packet
    from ..strategy_governance.repository import create_issue
    from ..strategy_governance.rules import digest
    rows=clean(load_rows(database,day,policy.horizon),str(day));result=evaluate(rows,str(day),policy)
    by=defaultdict(list)
    for row in rows:by[cohort(row)].append(row)
    issues=[]
    for group in result['groups']:
        key=(group['lane'],group['profile'],group['regime'],group['source_kind'],group['timing'])
        variants=['attention','recent_strength']
        if group['lane']=='trend':variants+=['ma_penalty_half']
        if group['lane']=='accumulation':variants+=['flow_15pct']
        group['shadow']=[shadow_compare(by[key],v,policy,as_of=str(day)) for v in variants]
        if not group['finding'] or not write:continue
        # One observed problem per cohort. Candidate fixes stay hypotheses and
        # go to independent review; never pick the best historical variant.
        identity=digest([VERSION,*key])
        issue=dict(title=f"{group['lane']} 前排后续表现持续落后",problem='同版本同市场状态头部落后后排；不是因果结论。',
            hypothesis='独立复核行业集中、市场环境、成交与样本偏差，再比较有限排序变体。',
            scope=group['lane'],out_of_scope='不改生产参数、不改变资格、不自动启用、不冒充实盘收益。',
            dedupe_key='effectiveness:'+identity,evidence=[{k:v for k,v in group.items() if k!='shadow'}],dependencies=[],
            change_kind='code',proposal_purpose='predictive',
            effectiveness_request={'profile':group['profile'],'regime':group['regime'],'source_kind':group['source_kind'],
                                   'variant':'attention','horizon':policy.horizon})
        raw=archive_json({'as_of_date':str(day),'rows':by[key]})
        reference=write_packet(issue_key=issue['dedupe_key'],question=issue['problem'],
            cases=[{'finding_evidence':p} for p in group['pairs'][:6]],
            code=[code_excerpt(Path(__file__).with_name('rules.py'),['finding=','def daily_pairs'],maximum=40)],
            facts={'aggregate':{k:v for k,v in group.items() if k not in ('pairs','shadow')},'original_rows':raw},
            limitations='前排与后排有行业相关性，必须独立质疑；历史排名对照不是成交或因果效果。')
        issue['review_evidence']=reference
        actor={'id':'system:effectiveness','roles':['observer']}
        item=create_issue(database,issue,actor)
        old=item.get('review_evidence') or item['issue'].get('review_evidence') or {}
        if item['state']=='discovered' and old.get('sha256')!=reference['sha256']:
            item=attach_review_packet(database,item['id'],item['revision'],reference,actor)
        issues.append(item['id'])
    # Keep source artifacts bounded outside the human-facing JSON.
    result['issue_ids']=issues
    if write:result['evidence_artifact']=archive_json({'as_of_date':str(day),'rows':rows,'result':result})
    for group in result['groups']:group.pop('pairs',None)
    result['row_count']=len(rows)
    result['manual_rows']=sum(r['source_kind']=='manual' for r in rows)
    result['manual_notice']='人工建议需用登记入口绑定原始候选与真实时间；未登记的聊天意见不自动归功或追认。'
    return result
