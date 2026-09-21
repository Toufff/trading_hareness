"""Append-only observation ledger; refreshed independently from today's selection."""
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict
from psycopg.types.json import Json
from .tracking_rules import origins, evaluate, digest, VERSION


def persist_origins(database, records):
    if not records:return 0
    with database.transaction() as c:
        with c.cursor() as cur:
            cur.executemany('''INSERT INTO quant.strategy_observation_origins
                (origin_id,signal_date,symbol,lane,profile,evidence) VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(origin_id) DO NOTHING''',
                [(r['origin_id'],r['signal_date'],r['symbol'],r['lane'],r['profile'],Json(r)) for r in records])
    return len(records)


def import_published_runs(database, day):
    """Retain original summaries, never rerun yesterday with today's parameters."""
    with database.transaction() as c:
        runs=c.execute('''SELECT run_id,updated_at,summary->'strategy_lanes' AS scan
            FROM quant.post_close_strategy_runs WHERE as_of_date BETWEEN %s AND %s
            AND summary ? 'strategy_lanes' ORDER BY as_of_date,updated_at''',(day-timedelta(days=60),day)).fetchall()
    records=[]
    for r in runs:
        scan=r['scan']
        if scan.get('lanes'):
            records.extend(origins(scan,r['updated_at'].isoformat(),'historical_run:'+str(r['run_id'])))
    return persist_origins(database,records)


def refresh(database, result, day, *, write=True):
    if write:
        import_published_runs(database,day)
        if result.get('status')=='completed':
            now=datetime.now(timezone.utc).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).isoformat()
            persist_origins(database,origins(result,now,'live_scan'))
    with database.transaction() as c:
        records=c.execute('''SELECT evidence FROM quant.strategy_observation_origins
            WHERE signal_date BETWEEN %s AND %s ORDER BY signal_date DESC,origin_id''',
            (day-timedelta(days=60),day)).fetchall()
        if not records:
            return dict(status='empty',as_of_date=str(day),version=VERSION,items=[],total=0,note='暂无冻结观察，不代表策略成功或失败。')
        old=[r['evidence'] for r in records];earliest=min(scan_dated(r)['signal_date'] for r in old)
        calendar=c.execute('''SELECT calendar_date,bool_or(is_open) AS is_open
            FROM quant.market_trade_calendar WHERE exchange IN ('SSE','SZSE')
            AND calendar_date>%s AND calendar_date<=%s GROUP BY calendar_date ORDER BY calendar_date''',(earliest,day)).fetchall()
        # Stored SSE calendar omits regular weekends, but every weekday must
        # explicitly say open/closed; never invent an open holiday/missing day.
        known={r['calendar_date'] for r in calendar}
        begin=date.fromisoformat(earliest)
        calendar_complete=all(begin+timedelta(days=i) in known for i in range(1,(day-begin).days+1)
                              if (begin+timedelta(days=i)).weekday()<5)
        sessions=[str(r['calendar_date']) for r in calendar if r['is_open']]
        symbols=sorted({r['symbol'] for r in old})
        bars=c.execute('''SELECT b.symbol,b.trading_date,f.raw->'screen_snapshot' AS snapshot,
            b.open,b.high,b.low,b.close,b.pre_close,b.limit_up,b.limit_down,b.adj_factor,b.is_suspended,
            b.amount*1000 AS amount
            FROM quant.canonical_bars_daily b LEFT JOIN quant.stock_money_flow_daily f
              ON b.symbol=f.symbol AND b.trading_date=f.trading_date
              AND f.source='longhuvip_main_net' AND f.raw ? 'screen_snapshot'
            WHERE b.symbol=ANY(%s) AND b.trading_date>=%s AND b.trading_date<=%s
            ORDER BY b.trading_date''',(symbols,earliest,day)).fetchall()
    by=defaultdict(list)
    for b in bars:
        by[b['symbol']].append({**(b['snapshot'] or {}),**{k:v for k,v in b.items() if k not in ('snapshot','symbol')},'trading_date':str(b['trading_date'])})
    ledger=[]
    for o in map(scan_dated,old):
        e=evaluate(o,sessions,by[o['symbol']],str(day))
        e['calendar_complete']=calendar_complete
        if not calendar_complete:e['status']='calendar_gap'
        e['lane_label']=o['lane_label'];e['source']=o['source'];e['profile']=o['profile'];e['available_at']=o.get('available_at')
        ledger.append(e)
    if write and ledger:
        with database.transaction() as c:
            with c.cursor() as cur:
                cur.executemany('''INSERT INTO quant.strategy_observation_evaluations
                    (origin_id,as_of_date,model_version,evidence_hash,evidence) VALUES(%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING''',[(e['origin_id'],day,VERSION,digest(e),Json(e)) for e in ledger])
    items=collapse(ledger)
    # Yesterday's displayed successes AND failures come first, never survivors only.
    items.sort(key=lambda e:(e['display_rank'] is None,-date.fromisoformat(e['signal_date']).toordinal(),e['display_rank'] or e['rank'],e['origin_id']))
    return dict(status='completed' if calendar_complete else 'calendar_gap',as_of_date=str(day),version=VERSION,
        total=len(items),ledger_rows=len(ledger),items=items,calendar_complete=calendar_complete,
        note='冻结发现日与原条件；第1/3/5/10交易日跟踪。历史补录单独标记，不计作当时真实成交或实盘胜率。')


def scan_dated(origin):
    """Manual recommendations registered after midnight (before 2026-09-17) stored the
    registration date as signal_date while their reference close is the scan's close.
    Evaluate them from the scan date; the ledger row itself is left as recorded."""
    scan_date=(origin.get('manual') or {}).get('source_scan_date')
    if scan_date and scan_date!=origin.get('signal_date'):
        return {**origin,'signal_date':scan_date,'signal_date_recorded':origin['signal_date']}
    return origin


def collapse(evaluations):
    """One displayed row per discovery: earlier ledger rows were appended on every
    pipeline retry and review closure (identity now excludes those), and the same
    close is also re-imported from the persisted run. The ledger keeps every row;
    the display prefers the prospective record, then the earliest availability."""
    best={}
    for e in evaluations:
        kind='manual' if str(e.get('source') or '').startswith('manual') else 'scan'
        key=(e['symbol'],e['signal_date'],e['lane'],kind)
        cur=best.get(key)
        rank=(e.get('timing')!='prospective',e.get('available_at') or '',e['origin_id'])
        if cur is None or rank<(cur.get('timing')!='prospective',cur.get('available_at') or '',cur['origin_id']):
            best[key]=e
    return list(best.values())
