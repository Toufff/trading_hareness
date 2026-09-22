"""Read-only owner DB replay; no delivery or model call unless explicitly selected."""
from __future__ import annotations
import argparse
import asyncio
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))
from app.db_dsn import connection_params
from app.intraday_advisory.pressure import pressure_event, feature_bundle, VERSION
from app.intraday_advisory.rules import sample_from_row
from app.intraday_advisory.renderer import signal_card, analysis_card
from app.intraday_advisory.presentation import ensure_readable_card
from app.intraday_advisory.delta import bind_output
from app.intraday_quote_normalization import exchange_time_status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',required=True)
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--provider',choices=('deepseek','codex'))
    args = parser.parse_args()
    config = dict(line.split('=',1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines()
                  if '=' in line and not line.startswith('#'))
    import psycopg
    from psycopg.rows import dict_row
    start = datetime.fromisoformat(args.date).replace(tzinfo=ZoneInfo('Asia/Shanghai'))
    with psycopg.connect(**connection_params(config),row_factory=dict_row,connect_timeout=5,
            options='-c default_transaction_read_only=on -c statement_timeout=30000') as db:
        rows = db.execute("""SELECT symbol,observed_at,raw FROM quant.intraday_quote_observations
            WHERE observed_at>=%s AND observed_at<%s AND source_name='longhu_order_book'
              AND symbol IN (SELECT DISTINCT s->>'symbol'
                FROM quant.intraday_advisory_analysis_runs r,
                     LATERAL jsonb_array_elements(r.input_payload->'scope') s
                WHERE r.started_at>=%s AND r.started_at<%s)
            ORDER BY observed_at,symbol LIMIT 100000""",(start,start+timedelta(days=1),start,start+timedelta(days=1))).fetchall()
        old_count = db.execute("""SELECT count(*) AS n FROM quant.intraday_advisory_deliveries
            WHERE created_at>=%s AND created_at<%s AND status='sent'""",(start,start+timedelta(days=1))).fetchone()['n']
    if len(rows)>=100000:
        raise ValueError('replay_row_limit_reached')
    histories = defaultdict(lambda:deque(maxlen=600))
    previous = {}
    events, quality = [], Counter()
    example_payload = None
    for row in rows:
        raw = row['raw'] or {}
        clock = exchange_time_status({'price_trade_time':raw.get('trade_time')},row['observed_at'],20)
        if clock.get('status') != 'fresh':
            quality[clock.get('status')]+=1
            continue
        at = datetime.fromisoformat(clock['observed_trade_time'])
        sample = sample_from_row(raw,at)
        if sample is None:
            quality['invalid_sample']+=1
            continue
        history = histories[sample.symbol]
        if history and at<=history[-1].observed_at:
            quality['duplicate_clock']+=1
            continue
        if history and (sample.amount<history[-1].amount or sample.volume_lot<history[-1].volume_lot):
            history.clear()
        history.append(sample)
        windows = feature_bundle(list(history))
        quality[windows['60']['status']]+=1
        event = pressure_event(list(history),previous.get(sample.symbol))
        if not event:
            continue
        card = signal_card(event,source='recommendation')
        ensure_readable_card(card)
        previous[sample.symbol]={'metrics':event.metrics}
        events.append({'event':asdict(event),'card':card})
        if example_payload is None:
            example_payload={'as_of':at.isoformat(),'report_kind':'event','trigger_kind':'historical_replay',
                'research_only':True,'live_orders':False,'delta_only':True,'market_context':{},'market_events':[],
                'recent_events':[{'symbol':sample.symbol,'summary':event.summary}],
                'scope':[{'symbol':sample.symbol,'name':sample.name,'scope':'recommendation',
                          'position_or_recommendation':{'buy_authorized':False,'trigger':'等待原条件确认，不据单次波动追价'},
                          'windows':windows,'previous_notified':None,
                          'quote':{'price':sample.price,'observed_at':at.isoformat()}}]}
    result = {'mode':'historical_readonly_replay','feature_version':VERSION,'date':args.date,
              'source_rows':len(rows),'old_sent_cards_all_types':old_count,
              'new_deterministic_cards':len(events),'quality':dict(quality),
              'states':dict(Counter(x['event']['metrics']['pressure_state'] for x in events)),
              'events':events,'model_payload_example':example_payload,
              'limitations':['Replay does not prove live acquisition continuity or profitability.',
                             'Previous transport assumed successful; no live sends or DB writes.']}
    if args.provider and example_payload:
        os.environ.update(config)
        from app.intraday_advisory.model import DeepSeekAdvisoryModel, CodexAdvisoryModel
        model = DeepSeekAdvisoryModel() if args.provider=='deepseek' else CodexAdvisoryModel()
        response = asyncio.run(model.analyze(example_payload))
        output = bind_output(response.output,example_payload)
        card = analysis_card(args.provider,output,report_kind='event',generated_at=datetime.now(ZoneInfo('Asia/Shanghai')))
        ensure_readable_card(card)
        result['live_model_preflight']={'provider':args.provider,'duration_ms':response.duration_ms,
                                         'model':response.model,'output':output,'card':card,'status':'passed'}
    target = Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in {'events','model_payload_example','live_model_preflight'}},ensure_ascii=False))
    if 'live_model_preflight' in result:
        print(json.dumps({k:v for k,v in result['live_model_preflight'].items() if k not in {'output','card'}},ensure_ascii=False))


if __name__=='__main__':
    main()
