"""Bounded, content-addressed original evidence packets for independent reviewers.

Missing evidence is a deterministic host wait, not a recurring model question.
Packets preserve real excerpts, not a claim that the observer's hypothesis is true.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
from .rules import require, digest, actor_role

MAX_PACKET_BYTES = 80_000
MAX_SOURCE_BYTES = 40_000_000
SECRET_KEY = re.compile(r'token|password|secret|api.?key|authorization|cookie|database_url|dsn|account',re.I)
SECRET_VALUE = re.compile(r'(?i)(Bearer\s+\S+|(?:sk-|sra_v\d_)[A-Za-z0-9_-]+|(?:token|password|api_key|secret)=([^\s&"\']+))')


def sanitize(value):
    if isinstance(value,dict):
        return {str(k):('[REDACTED]' if SECRET_KEY.search(str(k)) else sanitize(v)) for k,v in value.items()}
    if isinstance(value,list): return [sanitize(v) for v in value]
    if isinstance(value,str): return SECRET_VALUE.sub('[REDACTED]',value)
    return value


def evidence_root():
    return Path(os.getenv('QUANT_GOVERNANCE_EVIDENCE_ROOT','G:/StockPlatform/data/research/governance-evidence')).resolve()


def _bytes(value):
    return json.dumps(sanitize(value),ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False).encode('utf-8')


def archive_json(value, root=None):
    root=Path(root or evidence_root()).resolve();root.mkdir(parents=True,exist_ok=True)
    raw=_bytes(value);require(len(raw)<=MAX_SOURCE_BYTES,'Evidence extract too large')
    sha=hashlib.sha256(raw).hexdigest();path=root/(sha+'.json')
    if not path.exists():
        with path.open('xb') as file:file.write(raw)
    require(hashlib.sha256(path.read_bytes()).hexdigest()==sha,'Archive hash conflict')
    return {'path':str(path),'sha256':sha,'bytes':len(raw)}


def code_excerpt(path, needles, *, before=3, after=8, maximum=50):
    """Archive exact selected original lines, not the mutable release-path reference."""
    path=Path(path);raw=path.read_bytes();lines=raw.decode('utf-8-sig').splitlines()
    hits=[i for i,line in enumerate(lines) if any(needle in line for needle in needles)]
    indices=sorted({n for i in hits for n in range(max(0,i-before),min(len(lines),i+after+1))})[:maximum]
    return {'source_name':path.name,'source_sha256':hashlib.sha256(raw).hexdigest(),
        'line_count':len(lines),'excerpt':[{'line':i+1,'text':sanitize(lines[i])} for i in indices],
        'excerpt_complete':len(indices)<maximum,'scope':'bounded original source lines; not complete module'}


def write_packet(*, issue_key, question, cases, code, facts, limitations, source_hashes=None,
                 status='ready', reason=None, root=None):
    require(status in ('ready','missing'),'Invalid evidence state')
    require(isinstance(cases,list) and len(cases)<=6,'At most 6 bounded cases per atomic issue')
    require(isinstance(code,list) and len(code)<=4,'At most 4 code excerpts')
    packet={'schema':'strategy-review-evidence-v1','issue_key':issue_key,'question':question,
        'status':status,'reason':reason,'cases':cases,'code':code,'facts':facts,
        'source_hashes':source_hashes or {},'limitations':limitations,
        'interpretation':'原始事实可供独立质疑；不表示假设已确认，也不表示修改已通过收益验证。'}
    raw=_bytes(packet)
    require(len(raw)<=MAX_PACKET_BYTES,'Review packet exceeds bounded model budget; split the issue/evidence')
    if status=='ready':require(bool(cases) and any(x.get('excerpt') for x in code),'Ready packet requires raw cases and original code, not paths/descriptions only')
    reference=archive_json(packet,root)
    return {**reference,'status':status,'schema':packet['schema'],'issue_key':issue_key,'reason':reason}


def load_review_packet(item, *, roots=None):
    """Read and validate before leasing/spending tokens; never follow paths supplied by a model."""
    reference=item.get('review_evidence') or item.get('issue',{}).get('review_evidence')
    if not isinstance(reference,dict):return {'status':'missing','reason':'waiting_original_evidence: no archived review packet'}
    expected_key=item.get('issue',{}).get('dedupe_key')
    if not expected_key or reference.get('issue_key')!=expected_key:
        return {'status':'invalid','reason':'review_packet_issue_mismatch','evidence_hash':reference.get('sha256')}
    if reference.get('status')!='ready':return {'status':'missing','reason':reference.get('reason') or 'waiting_original_evidence','evidence_hash':reference.get('sha256')}
    try:
        path=Path(reference['path']).resolve()
        allowed=[Path(p).resolve() for p in (roots or [evidence_root()])]
        require(any(root in path.parents for root in allowed),'Review packet is outside configured evidence archive')
        require(path.is_file() and path.stat().st_size<=MAX_PACKET_BYTES,'Missing or oversized original review packet')
        raw=path.read_bytes()
        require(hashlib.sha256(raw).hexdigest()==reference['sha256'],'Review packet hash mismatch')
        packet=json.loads(raw.decode('utf-8-sig'))
        validate_review_packet(packet,item)
        return {'status':'ready','packet':packet,'evidence_hash':reference['sha256']}
    except (ValueError,KeyError,OSError,TypeError) as exc:
        return {'status':'invalid','reason':'invalid_original_evidence: '+str(exc),'evidence_hash':reference.get('sha256')}


def validate_review_packet(packet,item):
    """Same strict contract for explicit host packets; no free-form dict shortcut."""
    require(isinstance(packet,dict),'Review packet must be an object')
    require(len(_bytes(packet))<=MAX_PACKET_BYTES,'Oversized review packet')
    require(bool(item.get('issue',{}).get('dedupe_key')) and packet.get('issue_key')==item['issue']['dedupe_key'],'Original review packet belongs to another atomic issue')
    require(packet.get('schema')=='strategy-review-evidence-v1' and packet.get('status')=='ready','Invalid review packet schema/state')
    cases=packet.get('cases')
    require(isinstance(cases,list) and 1<=len(cases)<=6 and all(isinstance(x,dict) for x in cases),'Original cases missing')
    require(any(set(c)&{'candidate','raw_flow_rows','raw_ohlc','before','after','coverage','finding_evidence'} for c in cases),'Original data fields missing; paths/summaries are not evidence')
    code=packet.get('code',[])
    require(isinstance(code,list) and 1<=len(code)<=4 and any(c.get('excerpt') for c in code),'Original code excerpt missing')
    for source in code:
        sha=source.get('source_sha256','')
        require(len(sha)==64 and all(c in '0123456789abcdef' for c in sha),'Original code source SHA256 required')
        require(isinstance(source.get('excerpt'),list) and len(source['excerpt'])<=100,'Unbounded code excerpt')
    require(packet==sanitize(packet),'Unredacted sensitive content in review packet')
    return packet


def attach_review_packet(database,item_id,expected_revision,reference,actor):
    """Append evidence revision without rewriting the original discovery/history."""
    from .repository import _get,_save
    actor_role(actor,'observer')
    with database.transaction() as c:
        item=_get(c,item_id,lock=True)
        require(item['state']=='discovered','Evidence change after review requires independent rework first')
        require(item['revision']==expected_revision,'Evidence revision conflict')
        previous=item.get('review_evidence') or {}
        if all(previous.get(k)==reference.get(k) for k in ('sha256','issue_key','schema','status')):return item
        if reference.get('status')=='ready':
            require(load_review_packet({**item,'review_evidence':reference})['status']=='ready','Cannot attach unverifiable packet')
        changed=deepcopy(item);changed['review_evidence']=deepcopy(reference);changed['revision']+=1
        changed.setdefault('evidence_revisions',[]).append({'revision':changed['revision'],'sha256':reference.get('sha256'),
            'status':reference.get('status'),'actor':actor['id']})
        _save(c,changed,'attach_evidence',actor)
    return changed


def compact_candidate(row):
    keys=('symbol','name','state','rank_score','reason','confirmation','invalidation','metrics','price_volume')
    result={k:deepcopy(row[k]) for k in keys if k in row}
    metrics=result.get('metrics') or {}
    for key in ('bars','flow_series','return_series','accumulation'):metrics.pop(key,None)
    return sanitize(result)


def archive_scan_finding(result,finding,root=None):
    symbols={s for e in finding['evidence'] for s in e.get('symbols',[])}
    lane=next((l for l in result.get('lanes',[]) if l['key']==finding['scope']),None)
    rows=(lane.get('selected',[])+lane.get('caution_list',[])) if lane else []
    cases=[{'source':'current_scan','candidate':compact_candidate(r)} for r in rows if not symbols or r['symbol'] in symbols][:4]
    if not cases:
        cases=[{'source':'current_scan_status','status':result.get('status'),'coverage':result.get('coverage'),
            'finding_evidence':finding['evidence'],'followup_status':(result.get('followup') or {}).get('status')}]
    app=Path(__file__).resolve().parents[1]
    code=[code_excerpt(app/'short_term_lanes'/'governance_checks.py',['def findings','missing=','contradiction=','followup='],maximum=45)]
    return write_packet(issue_key=finding['dedupe_key'],question=finding['problem'],cases=cases,code=code,
        facts={'as_of_date':result.get('as_of_date'),'strategy_version':result.get('version'),
               'original_result_hash':digest(result),'code_fingerprint_scope':'exact included code excerpt source hashes'},
        limitations=['这是当轮输出一致性核查，不能直接推断交易有效性。','原始数据为有界摘录，未提供的字段不视为已验证。'],root=root)
