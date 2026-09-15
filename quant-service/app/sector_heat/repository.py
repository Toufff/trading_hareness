"""Point-in-time sector observations from PostgreSQL only; no SQLite fallback."""
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')
PHASES = (('open30',930),('midday',1130),('afternoon30',1300),('tail',1400),('close',1500))
ENDPOINTS = ('longhu_board_industry','longhu_board_concept')


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed).astimezone(SHANGHAI)
    except (ValueError, TypeError):
        return None


def phase_for(value):
    parsed = timestamp(value)
    return next((name for name,start in reversed(PHASES) if parsed and parsed.hour*100+parsed.minute>=start),None)


def payload(value):
    if isinstance(value,dict):
        return value
    try:
        result=json.loads(value or '{}')
        return result if isinstance(result,dict) else {}
    except (ValueError,TypeError):
        return {}


def load(conn, trade_date, as_of, history_days=20, phase=None):
    cutoff=timestamp(as_of)
    requested=trade_date or cutoff.date().isoformat()
    start=(datetime.fromisoformat(requested)-timedelta(days=40)).date().isoformat()
    endpoint_rows=conn.execute("""SELECT payload FROM quant.legacy_source_records
        WHERE source_table='source_endpoints' AND payload->>'endpoint_key'=ANY(%s)""",(list(ENDPOINTS),)).fetchall()
    endpoints={str(r['payload']['id']):r['payload']['endpoint_key'] for r in endpoint_rows}
    legacy=conn.execute("""SELECT payload FROM quant.legacy_source_records WHERE source_table='source_observations'
        AND payload->>'endpoint_id'=ANY(%s) AND payload->>'trade_date' BETWEEN %s AND %s""",
        (list(endpoints),start,requested)).fetchall()
    records=[]
    for result in legacy:
        r=dict(result['payload']); when=timestamp(r.get('available_at')); received=timestamp(r.get('received_at'))
        body=payload(r.get('payload_json'))
        if not when or not received or max(when,received)>cutoff or when.date().isoformat()!=r.get('trade_date') or body.get('period') not in (None,'today'):
            continue
        kind=endpoints[str(r['endpoint_id'])].removeprefix('longhu_board_')
        code=str(body.get('code') or r['scope_key'])
        records.append({**r,'id':str(r['id']),'body':body,'when':when,'received':received,
                        'phase':phase_for(when),'kind':kind,'code':code,'name':str(body.get('name') or code),
                        'key':f'{kind}:{code}','series_basis':'vendor_board'})
    reports=conn.execute("""SELECT board_report_id,observed_at,source_status,payload FROM quant.intraday_board_reports
        WHERE status='completed' AND source_status->>'provider'='longhuvip_composite'
          AND source_status->>'trade_date' BETWEEN %s AND %s AND observed_at<=%s ORDER BY observed_at""",
        (start,requested,cutoff)).fetchall()
    for report in reports:
        meta=report['source_status']; day=meta['trade_date']; when=timestamp(meta.get('data_as_of'))
        if not when: continue
        for b in report['payload'].get('items',[]):
            # Older backfills accidentally attached the live board quote.
            if b.get('source')!='longhuvip:dated_member_aggregate': continue
            code=str(b['sector_key'])
            body={'amount':b.get('amount'),'main_net':b.get('net_inflow'),'change_pct':b.get('change_pct'),
                  'volume_ratio':b.get('volume_ratio'),'advancing_breadth':b.get('advancing_breadth'),
                  'member_count':b.get('mapped_members')}
            records.append({'id':f"native:{report['board_report_id']}:{code}",'trade_date':day,'phase':'close',
                'kind':'industry','code':code,'name':b.get('label',code),'key':f'industry:{code}',
                'body':body,'when':when,'received':timestamp(report['observed_at']),
                'available_at':when.isoformat(),'received_at':report['observed_at'].isoformat(),'quality':'ok',
                'series_basis':'dated_member_aggregate'})
    market_days=conn.execute("""SELECT DISTINCT trading_date FROM quant.canonical_bars_daily
        WHERE trading_date BETWEEN %s AND %s""",(start,requested)).fetchall()
    days=sorted({r['trade_date'] for r in records} | {str(r['trading_date']) for r in market_days})
    phase_days=sorted({r['trade_date'] for r in records if not phase or r['phase']==phase})
    day=trade_date or (phase_days[-1] if phase_days else requested)
    axis=[d for d in days if d<=day][-max(3,min(22,int(history_days)+2)):]
    return day,cutoff,axis,[r for r in records if r['trade_date'] in axis],[]
