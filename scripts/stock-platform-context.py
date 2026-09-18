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


DEFAULT_HOT_DATA_DIR='G:/StockPlatform/data/postgresql16'
DEFAULT_COLD_TABLESPACE_DIR='G:/StockPlatform/data/pg-cold'


def read(base,path):
    with build_opener(ProxyHandler({})).open(base+path,timeout=30) as r:
        return json.load(r)


def read_env_file(path):
    file=Path(path)
    if not file.is_file():
        return {}
    return dict(l.split('=',1) for l in file.read_text(encoding='utf-8-sig').splitlines()
                if '=' in l and not l.startswith('#'))


def storage_layout(config):
    """Where the owner database actually is, per docs/OWNER_DATABASE_STORAGE.md.

    The hot cluster (tables, indexes, WAL, temp) moved off the G: HDD onto the
    NVMe tier; rows older than the hot window live in the `stock_cold`
    tablespace on G:, and the backup chain stays on G: plus off-site. Reporting
    only the old G: path here would send an agent to the wrong directory.
    """
    hot=(config.get('PGDATA_DIR') or DEFAULT_HOT_DATA_DIR).strip().replace('\\','/')
    cold=(config.get('PGDATA_COLD_TABLESPACE_DIR') or DEFAULT_COLD_TABLESPACE_DIR).strip().replace('\\','/')
    budget=(config.get('PGDATA_BUDGET_BYTES') or str(500*1024**3)).strip()
    try:
        budget_bytes=int(budget)
    except ValueError:
        budget_bytes=None
    return {'hot_data_directory':hot,'hot_budget_bytes':budget_bytes,
            'cold_tablespace':'stock_cold','cold_tablespace_directory':cold,
            'backup_root':'G:/StockPlatform/backups',
            'notice':'冷层 quant.*_cold 与 quant.*_all 视图只供运维查询，应用代码不得引用。'}


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
    readiness=health.get('daily_control_plane') or {}
    # Pass the per-exchange gate and the universe-drift diagnostics through
    # verbatim: a 'missing 113 daily bars' reading of a denominator change is
    # exactly what this block exists to prevent.
    drift={k:readiness.get(k) for k in ('by_exchange','gating_exchanges','ungated_exchanges','all_a',
        'expected_previous_trading_day','expected_previous_daily_rows','expected_delta','expected_sources')}
    config=read_env_file(a.env_file)
    layout=storage_layout(config)
    output={'system':'trading_hareness','authoritative_database':layout['hot_data_directory'],
            'database_storage':layout,
            'run_id':run.get('run_id'),'as_of_date':run.get('as_of_date'),
            'requested_date':a.date,'date_matches':not a.date or a.date==str(run.get('as_of_date')),
            'source_sha256':report.get('source_sha256'),
            'equity_readiness':readiness,'equity_universe_drift':drift,
            'strategy_status':lanes.get('status'),
            'market':lanes.get('market'),'review_coverage':lanes.get('review_coverage'),
            'reports':[{'key':r['key'],'file':str(Path('G:/StockPlatform/reports/short-term')/r['filename']),
                        'content_sha256':r['content_sha256']} for r in report.get('reports',[])],
            'lanes':[l for l in lanes.get('lanes',[]) if not a.lane or l['key']==a.lane]}
    if a.symbol:
        code=a.symbol.split('.')[0].removeprefix('sh').removeprefix('sz')
        if len(code)!=6 or not code.isdigit():
            p.error('symbol must be a six-digit stock code or canonical code')
        if not config:
            p.error('missing or empty runtime environment file: '+str(a.env_file))
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
