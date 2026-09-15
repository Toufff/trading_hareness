"""Read the production run and imported research history; never run legacy code."""
import argparse
import json
from pathlib import Path
import sys
from urllib.request import build_opener, ProxyHandler
import psycopg
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
from app.db_dsn import connection_params


def read(base,path):
    with build_opener(ProxyHandler({})).open(base+path,timeout=30) as r:
        return json.load(r)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:5681')
    p.add_argument('--env-file',default=r'G:\StockPlatform\config\runtime.env')
    p.add_argument('--date')
    p.add_argument('--lane')
    p.add_argument('--symbol')
    p.add_argument('--limit',type=int,default=10)
    a=p.parse_args()
    health=read(a.base_url,'/health')
    from urllib.parse import urlencode
    result=read(a.base_url,'/api/v1/strategy/post-close/latest'+('?' + urlencode({'as_of_date':a.date}) if a.date else ''))
    run=result.get('run') or {}
    lanes=(run.get('summary') or {}).get('strategy_lanes') or {}
    report=lanes.get('report_bundle') or {}
    output={'system':'trading_hareness','authoritative_database':'G:/StockPlatform/data/postgresql16',
            'run_id':run.get('run_id'),'as_of_date':run.get('as_of_date'),
            'requested_date':a.date,'date_matches':not a.date or a.date==str(run.get('as_of_date')),
            'source_sha256':report.get('source_sha256'),
            'equity_readiness':health.get('daily_control_plane'),
            'strategy_status':lanes.get('status'),
            'market':lanes.get('market'),'review_coverage':lanes.get('review_coverage'),
            'reports':[{'key':r['key'],'file':str(Path('G:/StockPlatform/reports/short-term')/r['filename']),
                        'content_sha256':r['content_sha256']} for r in report.get('reports',[])],
            'lanes':[l for l in lanes.get('lanes',[]) if not a.lane or l['key']==a.lane]}
    if a.symbol:
        code=a.symbol.split('.')[0].removeprefix('sh').removeprefix('sz')
        if len(code)!=6 or not code.isdigit():
            p.error('symbol must be a six-digit stock code or canonical code')
        config=dict(l.split('=',1) for l in Path(a.env_file).read_text(encoding='utf-8-sig').splitlines() if '=' in l and not l.startswith('#'))
        with psycopg.connect(**connection_params(config),row_factory=dict_row,
                            options='-c default_transaction_read_only=on -c statement_timeout=15000') as db:
            history=db.execute('''SELECT source_table,source_row_key,effective_at,available_at,payload_sha256,payload
                FROM quant.legacy_source_records WHERE source_table IN
                ('research_runs','research_gates','investment_theses','pool_memberships','short_term_scan_results','trade_setups','trade_setup_events')
                AND (payload->>'security_code' IN (%s,%s,%s,%s,%s) OR payload->>'code' IN (%s,%s,%s))
                ORDER BY available_at DESC NULLS LAST LIMIT %s''',
                (code,'sh'+code,'sz'+code,code+'.SH',code+'.SZ',code,'sh'+code,'sz'+code,max(1,min(a.limit,50)))).fetchall()
        output['historical_reference']={'symbol':a.symbol,'decision_eligible':False,
            'notice':'迁入的历史研究与跟踪事实，不是今天的结论。保持原始日期，须结合当前证据重新判断。','records':history}
    print(json.dumps(output,ensure_ascii=False,default=str))
    if not output['date_matches']:
        raise SystemExit(2)


if __name__=='__main__':main()
