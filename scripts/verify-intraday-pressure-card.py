"""Explicit one-card acceptance using the production Feishu transport."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))
from app.feishu_custom_bot import post_custom_bot_card
from app.feishu_direct_alert import post_direct_feishu_alert_card
from app.intraday_advisory.presentation import ensure_readable_card
from app.feishu_card_v2 import markdown


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--replay-file',required=True)
    parser.add_argument('--receipt',required=True)
    parser.add_argument('--send-test',action='store_true')
    args=parser.parse_args()
    replay=json.loads(Path(args.replay_file).read_text(encoding='utf-8'))
    card=replay['events'][0]['card']
    card['header']['title']['content']='盘中卡片新版验收｜历史回放'
    card['header']['subtitle']['content']=replay['date']+' 历史数据，非当前交易提醒'
    card['config']['summary']['content']='测试消息：新版增量卡片已部署，以下为历史回放，不是买卖建议'
    card['body']['elements'].insert(0,markdown('**测试消息｜历史回放，不是当前行情或买卖建议。**'))
    ensure_readable_card(card)
    result={'mode':'validate_only','card_validated':True}
    if args.send_test:
        config=dict(line.split('=',1) for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines()
                    if '=' in line and not line.startswith('#'))
        transport=post_custom_bot_card if config.get('FEISHU_ALERT_TRANSPORT')=='custom_bot' else post_direct_feishu_alert_card
        result=asyncio.run(transport(card,environ=config))
    target=Path(args.receipt)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps({'checked_at':datetime.now().astimezone().isoformat(),'result':result,'card':card},
                                 ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result.get('status',result.get('mode')),'card_validated':True},ensure_ascii=False))
    if args.send_test and result.get('status')!='sent':
        raise SystemExit(1)


if __name__=='__main__':main()
