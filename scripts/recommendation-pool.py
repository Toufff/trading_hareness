"""Prepare, review, publish and export ONE recommendation decision; no THS writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(body, encoding='utf-8')
    temp.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'publish', 'plan'])
    p.add_argument('--date', required=True)
    p.add_argument('--snapshot', type=Path)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--review', type=Path)
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    a = p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(a.env_file, override=True)
    from app.database import Database
    from app.strategy_read_model import latest_post_close_strategy
    from app.intraday_evidence_read_model import watchlists
    from app.recommendation_pool.rules import intake, sync_plan, current_view
    from app.recommendation_pool.repository import persist
    from app.recommendation_pool.report import markdown
    db = Database()
    try:
        payload = latest_post_close_strategy(db, a.date)
        run = payload.get('latest_completed') or {}
        scan = (run.get('summary') or {}).get('strategy_lanes') or {}
        if str(run.get('as_of_date')) != a.date or scan.get('status') != 'completed':
            raise ValueError('no_completed_exact_date_scan')
        groups = {}
        if a.snapshot:
            receipt = json.loads(a.snapshot.read_text(encoding='utf-8-sig'))
            raw = receipt.get('after') or receipt.get('before')
            for g in raw['group_list']:
                codes, markets = g['content'].split(',')
                suffix = {'17': 'SH', '33': 'SZ', '20': 'SHETF', '36': 'SZETF'}
                groups[g['name']] = [c + '.' + suffix.get(m, m) for c, m in zip(filter(None, codes.split('|')), filter(None, markets.split('|')))]
        if a.command == 'prepare':
            if not a.snapshot:
                raise ValueError('fresh_ths_snapshot_required')
            tracked = [r['symbol'] for r in watchlists(db).get('items', []) if any(t.get('source') == 'user' and t.get('active') is not False for t in (r.get('metadata') or {}).get('tracking_tags', []))]
            with db.transaction() as c:
                nxt = c.execute("SELECT min(calendar_date) AS day FROM quant.market_trade_calendar WHERE exchange='SSE' AND is_open AND calendar_date>%s", (a.date,)).fetchone()
            if not nxt or not nxt['day']:
                raise ValueError('next_trading_session_missing')
            context = intake(scan, run['run_id'], groups, tracked, nxt['day'])
            write(a.directory / 'context.json', context)
            write(a.directory / 'review-template.json', {'context_hash': context['context_hash'], 'author': '', 'market_assessment': '', 'attention_budget': 5, 'items': []})
            print(json.dumps({'context_hash': context['context_hash'], 'candidate_count': context['candidate_count'], 'required_reviews': context['required_reviews']}, ensure_ascii=False))
        elif a.command == 'publish':
            if not a.review:
                raise ValueError('review_required')
            context = json.loads((a.directory / 'context.json').read_text(encoding='utf-8'))
            review = json.loads(a.review.read_text(encoding='utf-8-sig'))
            bundle = persist(db, context, review)
            write(a.directory / 'decision.json', bundle)
            write(a.directory / '推荐决策.md', markdown(bundle))
            print(json.dumps({'decision_id': bundle['decision_id'], 'status': bundle['status'], 'coverage': bundle['coverage']}, ensure_ascii=False))
            if not bundle['sync_allowed']:
                return 2
            from app.recommendation_pool.followup import register_decision
            followup = register_decision(db, scan, bundle)
            write(a.directory / 'followup-registration.json', followup)
        else:
            if not a.snapshot:
                raise ValueError('fresh_ths_snapshot_required')
            bundle = run['summary'].get('recommendation_pool') or {}
            bundle = current_view(scan, bundle)
            plan = sync_plan(bundle, groups)
            write(a.directory / 'ths-plan.json', plan)
            print(json.dumps(plan, ensure_ascii=False))
    finally:
        db.close()
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
