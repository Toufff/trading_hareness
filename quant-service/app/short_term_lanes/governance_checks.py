"""Deterministic inspection, atomic deduped issues; never mutates strategy results."""
from collections import Counter


def findings(result):
    version=result.get('version','unknown')
    evidence_base={'as_of_date':result.get('as_of_date'),'strategy_version':version}
    out=[]
    def add(key,title,problem,hypothesis,scope,evidence):
        out.append(dict(title=title,problem=problem,hypothesis=hypothesis,scope=scope,
            out_of_scope='不自动修改阈值、排序、持仓或生产配置；先由独立角色复核。',
            dedupe_key=f'{version}:{key}',evidence=[{**evidence_base,**evidence}],dependencies=[]))
    if result.get('status')!='completed':
        add('data-coverage','策略截面完整性不足','本轮扫描未完成，不能据此推断没有机会。',
            '检查缺失日期和采集覆盖，先排除数据问题再讨论策略修改。','data',{'coverage':result.get('coverage')})
    config=result.get('governance_config') or {}
    if config.get('status') in ('stale_code','unavailable'):
        add('governance-config:'+str(config.get('generation')),'已批准因子需要重新验证',
            config['note'],'检查代码指纹、部署基线和治理存储，独立策略不应因可选配置失败而停摆。',
            'governance_config',config)
    for lane in result.get('lanes',[]):
        items=lane.get('selected',[])+lane.get('caution_list',[])
        missing=[r['symbol'] for r in items if not r.get('price_volume')]
        if missing:
            add(lane['key']+':missing-pv',lane['label']+'缺少结构化量价证据',
                '展示候选没有同轮量价证据对象。','检查生成、持久化及展示链是否丢字段。',lane['key'],{'symbols':missing})
        contradiction=[r['symbol'] for r in items if r.get('price_volume',{}).get('status')=='quality_warning' and r.get('state')=='watch']
        if contradiction:
            add(lane['key']+':warning-priority',lane['label']+'量价警示与状态冲突',
                '存在量价警示却仍标为普通优先观察。','核查该警示应当降级还是仅作非排除信息，禁止自动放宽。',lane['key'],{'symbols':contradiction})
    followup=result.get('followup') or {}
    if followup.get('status') in ('failed','calendar_gap'):
        add('followup-gap','往期候选跟踪不完整','本轮候选后续评价没有完整运行。',
            '区分日历、缺行情与持久化错误，保留原样本。','tracking',{'status':followup.get('status')})
    gaps = [r for r in followup.get('items',[]) if r.get('status') in ('data_gap','adjustment_gap','calendar_gap')]
    if gaps:
        add('followup-row-gap','往期候选存在逐股数据缺口','顶层跟踪完成不代表每只股票完整。',
            '分别检查日线、复权与日历，不把缺值计作零收益。','tracking',
            {'symbols':sorted({r['symbol'] for r in gaps}), 'coverage':dict(total=followup.get('total'),gaps=len(gaps))})
    return out


def inspect_run(database,result):
    from ..strategy_governance.repository import create_issue
    from ..strategy_governance.review_evidence import archive_scan_finding,attach_review_packet
    issues=findings(result)
    ids=[]
    for issue in issues:
        frozen=result.get('governance_input') or {}
        if frozen.get('status')=='ready':
            # Provenance only: a data/code finding is not a factor-tuning request.
            issue['host_frozen_input']={k:frozen[k] for k in ('input_path','input_hash') if k in frozen}
        reference=archive_scan_finding(result,issue)
        issue['review_evidence']=reference
        snapshot=create_issue(database,issue,{'id':'system:postclose','roles':['observer']})
        existing=snapshot.get('review_evidence') or snapshot['issue'].get('review_evidence') or {}
        if snapshot['state']=='discovered' and existing.get('sha256')!=reference['sha256']:
            snapshot=attach_review_packet(database,snapshot['id'],snapshot['revision'],reference,{'id':'system:postclose','roles':['observer']})
        ids.append(snapshot['id'])
    return {'status':'completed','finding_count':len(issues),'issue_ids':ids,
            'live_effect':'none','scope':'数据和输出一致性检查，不代表策略盈利验收'}
