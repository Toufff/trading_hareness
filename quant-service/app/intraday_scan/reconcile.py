"""Forward observation changes, not backtested execution or realizable P&L."""
from datetime import datetime
from psycopg.types.json import Json
from .rules import digest, evaluate


def compare(source, closing_input, closing_result):
    cutoff=datetime.fromisoformat(closing_input['cutoff'])
    if cutoff.hour != 15 or source['cutoff'][:10] != closing_input['cutoff'][:10] or source['cutoff'] >= closing_input['cutoff']:
        raise ValueError('Comparison requires later same-day closing snapshot')
    if source.get('observed_at',source['cutoff']) >= closing_input['cutoff']:
        raise ValueError('Source was not available before close')
    quotes={r['symbol']:r for r in closing_input['rows']}
    now={(r['symbol'],r['lane']):r for lane in closing_result['lanes'] for r in lane['items']}
    result=[]
    for lane in source['lanes']:
        for item in lane['items']:
            q=quotes.get(item['symbol']); latest=now.get((item['symbol'],item['lane']))
            checked=evaluate(item,q,closing_input.get('minutes',{}).get(item['symbol'],[]),closing_input['cutoff'],0,
                             previous_plan=item.get('plan'),plan_available_at=closing_input['observed_at'])
            price=item.get('price'); end=q.get('close') if q else None
            result.append(dict(symbol=item['symbol'],name=item['name'],lane=item['lane'],
                source_state=item['state'],close_state=latest['state'] if latest else 'not_in_current_discovery',
                source_price=price,close_price=end,observation_change_pct=(end/price-1)*100 if end and price else None,
                plan_state=(checked.get('plan') or {}).get('state'),
                triggered_at=(checked.get('plan') or {}).get('triggered_at'),
                minute_evidence=checked.get('minute_end') is not None,fill_verified=False))
    return dict(source_input_hash=source['input_hash'],close_input_hash=closing_result['input_hash'],
        source_cutoff=source['cutoff'],close_cutoff=closing_input['cutoff'],items=result,
        meaning='从原观察价至收盘的价格变化，不是假设成交收益；涨跌停、费用及T+1均未用来虚构交易',
        fills_verified=0,source_mutated=False)


def persist_day(database,close_run_id):
    with database.transaction() as c:
        close=c.execute('SELECT input,result,cutoff FROM quant.intraday_strategy_scans WHERE run_id=%s AND state=%s',(close_run_id,'completed')).fetchone()
        if not close or datetime.fromisoformat(close['input']['cutoff']).hour != 15:raise ValueError('Missing completed closing snapshot')
        sources=c.execute('''SELECT run_id,result,input->>'observed_at' AS observed_at FROM quant.intraday_strategy_scans WHERE state='completed'
            AND cutoff<%s AND cutoff>=%s ORDER BY cutoff''',
            (close['cutoff'],datetime.fromisoformat(close['input']['cutoff']).replace(hour=9,minute=30))).fetchall()
        saved=0;skipped=[]
        for s in sources:
            try:result=compare({**s['result'],'observed_at':s['observed_at'] or s['result']['cutoff']},close['input'],close['result'])
            except ValueError as exc:
                skipped.append(dict(run_id=str(s['run_id']),reason=str(exc)));continue
            before=digest(s['result'])
            c.execute('''INSERT INTO quant.intraday_scan_reconciliations(source_run_id,close_run_id,result)
                VALUES(%s,%s,%s) ON CONFLICT DO NOTHING''',(s['run_id'],close_run_id,Json(result)))
            read=c.execute('SELECT result FROM quant.intraday_strategy_scans WHERE run_id=%s',(s['run_id'],)).fetchone()
            if digest(read['result'])!=before:raise ValueError('Source mutated')
            saved+=1
    return dict(compared=saved,skipped=skipped,close_run_id=str(close_run_id),source_readback_unchanged=True)
