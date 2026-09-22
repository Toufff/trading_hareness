"""Read-only SQL acceptance and historical card rendering; never sends a message."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))
from app.db_dsn import connection_params
from app.intraday_advisory.repository import notification_exists, signal_delivery_suppressed, latest_delivered_context
from app.intraday_advisory.rules import AdvisorySignal
from app.intraday_advisory.renderer import signal_card, analysis_card
from app.intraday_advisory.presentation import ensure_readable_card
from app.intraday_advisory.notice_policy import VERSION


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--replay-file',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    config=dict(line.split('=',1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines()
                if '=' in line and not line.startswith('#'))
    import psycopg
    from psycopg.rows import dict_row
    class Capture:
        def execute(self,sql,params):
            self.sql=sql
            return self
        def fetchone(self): return None
    capture=Capture()
    signal_delivery_suppressed(capture,'test',account_key='test')
    sql=capture.sql.replace('quant.intraday_advisory_events','fixture_e').replace(
        'quant.discipline_alert_events','fixture_de').replace('quant.discipline_alert_deliveries','fixture_d')
    cte="""WITH fixture_e AS (
        SELECT 'test'::text AS event_id,'600000.SH'::text AS symbol,%s::text AS scope_source,
               %s::text AS direction,now() AS observed_at),
        fixture_de AS (SELECT 'discipline'::text AS event_id,
               jsonb_build_object('symbol',%s::text,'account_key',%s::text) AS payload,
               now()+make_interval(secs=>%s) AS observed_at,%s::text AS line_kind,
               'triggered'::text AS to_state),
        fixture_d AS (SELECT 'discipline'::text AS event_id,%s::text AS status,0 AS attempt_count)
        """
    cases=[('same_episode', 'holding','down','600000.SH','test',0,'hard_stop','sent',True),
           ('candidate','recommendation','down','600000.SH','test',0,'hard_stop','sent',False),
           ('opposite','holding','up','600000.SH','test',0,'hard_stop','sent',False),
           ('other_stock','holding','down','600001.SH','test',0,'hard_stop','sent',False),
           ('other_account','holding','down','600000.SH','other',0,'hard_stop','sent',False),
           ('old_episode','holding','down','600000.SH','test',-120,'hard_stop','sent',False),
           ('profit_line','holding','down','600000.SH','test',0,'profit_take','sent',False),
           ('not_delivered','holding','down','600000.SH','test',0,'hard_stop','pending',False)]
    with psycopg.connect(**connection_params(config),row_factory=dict_row,connect_timeout=5,
                        options='-c default_transaction_read_only=on -c statement_timeout=10000') as db:
        assert db.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']=='on'
        assert not notification_exists(db,'notice-v2:read-only-acceptance:not-a-real-delivery')
        assert not signal_delivery_suppressed(db,'00000000-0000-0000-0000-000000000000',account_key='test')
        baseline_count=len(latest_delivered_context(db,at=datetime.now().astimezone()))
        for name,*values,expected in cases:
            actual=db.execute(cte+sql,(*values,'test','test')).fetchone() is not None
            if actual != expected:
                raise AssertionError(f'precedence mismatch: {name}')
    replay=json.loads(Path(args.replay_file).read_text(encoding='utf-8'))
    events=[]
    for row in replay['events']:
        values={**row['event'],'observed_at':datetime.fromisoformat(row['event']['observed_at'])}
        event=AdvisorySignal(**values)
        card=signal_card(event,source='recommendation')
        ensure_readable_card(card)
        events.append({'event':row['event'],'card':card})
    briefs=[]
    for kind in ('fixed','midday','tail'):
        card=analysis_card('codex',{'market_state':'calm','headline':'样式验收：本轮无新增计划变化',
            'market_summary':'合成测试内容，不是当前市场判断','holding_focus':[],
            'recommendation_focus':[],'risks':[]},report_kind=kind,generated_at=datetime.now().astimezone())
        ensure_readable_card(card)
        briefs.append(card)
    receipt={'version':VERSION,'checked_at':datetime.now().astimezone().isoformat(),
             'date':replay['date'],'mode':'readonly_sql_and_historical_render',
             'precedence_cases':len(cases),'delivered_context_symbols':baseline_count,
             'historical_cards_validated':len(events),'events':events,'synthetic_brief_previews':briefs}
    output=Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in receipt.items() if k not in {'events','synthetic_brief_previews'}},ensure_ascii=False))


if __name__=='__main__': main()
