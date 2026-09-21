"""Evaluate one stored, point-in-time paper context with JEV. Never execute it."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv
from app.agent_paper.jev import JevPaperModel, POLICY
from app.database import Database


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--provider-env-file', default='G:/StockPlatform/config/jev-paper.env')
    p.add_argument('--source-account', default='agent-codex-sol')
    p.add_argument('--decision-id')
    p.add_argument('--out-dir', type=Path, default=Path('G:/StockPlatform/reports/jev-pilot'))
    args = p.parse_args()
    load_dotenv(args.env_file, override=True)
    load_dotenv(args.provider_env_file, override=True)
    db = Database()
    try:
        with db.transaction() as c:
            if args.decision_id:
                row = c.execute('SELECT decision_id,account_key,decided_at,context FROM quant.agent_paper_decisions '
                                'WHERE decision_id=%s AND context IS NOT NULL', (args.decision_id,)).fetchone()
            else:
                row = c.execute('SELECT decision_id,account_key,decided_at,context FROM quant.agent_paper_decisions '
                                'WHERE account_key=%s AND context IS NOT NULL ORDER BY decided_at DESC LIMIT 1',
                                (args.source_account,)).fetchone()
        if row is None:
            raise SystemExit('No stored point-in-time context is available')
        result = JevPaperModel().decide(json.dumps(row['context'], ensure_ascii=False, default=str))
        payload = {'mode': 'historical_preview_only', 'orders_executed': False, 'policy': POLICY,
                   'source_decision_id': str(row['decision_id']), 'source_account': row['account_key'],
                   'source_as_of': row['decided_at'].isoformat(), 'model': result.model,
                   'duration_ms': result.duration_ms, 'usage': result.usage, 'output': result.output,
                   'transcript': result.transcript}
        args.out_dir.mkdir(parents=True, exist_ok=True)
        path = args.out_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-preview.json')
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in payload.items() if k != 'transcript'} | {'artifact': str(path)},
                         ensure_ascii=False, default=str))
    finally:
        db.close()


if __name__ == '__main__':
    main()
