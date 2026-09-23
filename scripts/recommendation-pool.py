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
    p.add_argument('--intraday-run-id', help='Use an exact 11:30 intraday scan for the noon decision')
    p.add_argument('--snapshot', type=Path)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--review', type=Path)
    p.add_argument('--strategy-report-dir', type=Path, default=Path('G:/StockPlatform/reports/short-term'),
                   help='After publish, export the enriched same-run strategy reports from the database')
    p.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    a = p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(a.env_file, override=True)
    from app.database import Database
    from app.strategy_read_model import latest_post_close_strategy
    from app.intraday_evidence_read_model import watchlists
    from app.recommendation_pool.rules import intake, sync_plan, current_view, note_template
    from app.recommendation_pool.repository import latest_target_groups, persist
    from app.recommendation_pool.report import markdown
    from app.recommendation_pool import noon
    db = Database()
    try:
        if a.intraday_run_id:
            scan, intraday_result, _, eligibility, cutoff = noon.load(db, a.intraday_run_id, a.date)
            run = {'run_id': a.intraday_run_id, 'as_of_date': a.date}
        else:
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
                groups = latest_target_groups(db, a.date)
            tracked = [r['symbol'] for r in watchlists(db).get('items', []) if any(t.get('source') == 'user' and t.get('active') is not False for t in (r.get('metadata') or {}).get('tracking_tags', []))]
            if a.intraday_run_id:
                next_session = a.date
            else:
                with db.transaction() as c:
                    nxt = c.execute("SELECT min(calendar_date) AS day FROM quant.market_trade_calendar WHERE exchange='SSE' AND is_open AND calendar_date>%s", (a.date,)).fetchone()
                if not nxt or not nxt['day']:
                    raise ValueError('next_trading_session_missing')
                next_session = nxt['day']
            context = intake(scan, run['run_id'], groups, tracked, next_session,
                             source_kind='noon' if a.intraday_run_id else 'post_close',
                             source_cutoff=cutoff.isoformat() if a.intraday_run_id else None,
                             evidence_eligibility=eligibility if a.intraday_run_id else None)
            write(a.directory / 'context.json', context)
            # The agent should spend its attention answering, not transcribing
            # ranks the scan already knows.  Every required review arrives as a
            # skeleton item and every candidate gets a note template, so a late
            # promotion does not need a second prepare run.
            names = {row['symbol']: row['name'] for row in context['candidates']}
            notes = {row['symbol']: note_template(context, row['symbol']) for row in context['candidates']}
            items = [{'symbol': symbol, 'name': names.get(symbol, symbol), 'data_date': context['as_of_date'],
                      'decision': '', 'stage': '', 'priority': None, 'why_now': '', 'comparison': '',
                      'invalidation': '', 'business': '', 'company_risk': '', 'sector_assessment': '',
                      'sector': '', 'trigger': '', 'peer_comparison': '', 'sources': [],
                      'recommendation_note': notes[symbol]}
                     for symbol in context['required_reviews']]
            if a.intraday_run_id:
                for item in items:
                    item['evidence_available_at'] = ''
            write(a.directory / 'review-template.json', {'context_hash': context['context_hash'], 'author': '',
                                                         'market_assessment': '', 'reviewed_at': '' if a.intraday_run_id else None,
                                                         'attention_budget': 5, 'items': items})
            write(a.directory / 'note-templates.json', notes)
            print(json.dumps({'context_hash': context['context_hash'], 'candidate_count': context['candidate_count'],
                              'required_reviews': context['required_reviews'],
                              'review_template_items': len(items), 'note_templates': len(notes),
                              'sector_overview_sectors': len(context.get('sector_overview') or {}),
                              'baseline_source': 'ths_snapshot' if a.snapshot else 'internal_recommendation_pool'}, ensure_ascii=False))
        elif a.command == 'publish':
            if not a.review:
                raise ValueError('review_required')
            context = json.loads((a.directory / 'context.json').read_text(encoding='utf-8'))
            review = json.loads(a.review.read_text(encoding='utf-8-sig'))
            bundle = noon.persist(db, context, review) if a.intraday_run_id else persist(db, context, review)
            if a.intraday_run_id:
                write(a.directory / 'decision.json', bundle)
                write(a.directory / '推荐决策.md', markdown(bundle, scan))
                print(json.dumps({'decision_id': bundle['decision_id'], 'status': bundle['status'],
                                  'coverage': bundle['coverage'], 'source_kind': 'noon'}, ensure_ascii=False))
                return 0 if bundle['sync_allowed'] else 2
            # Persist() enriches the existing same-run strategy publication
            # with this exact company research. Re-read it instead of using
            # the pre-publish snapshot held above.
            payload = latest_post_close_strategy(db, a.date)
            run = payload.get('latest_completed') or {}
            scan = (run.get('summary') or {}).get('strategy_lanes') or {}
            from app.short_term_lanes.reports import write_bundle
            write_bundle(a.strategy_report_dir, scan, scan['report_bundle'])
            # The review page carries the decision too; regenerate it from the
            # same re-read run so file and API show one decision_id.
            from app.short_term_lanes.review_page import write as write_review_page
            write_review_page(a.strategy_report_dir, {'run': run})
            write(a.directory / 'decision.json', bundle)
            # The human report carries the same persisted decision plus the
            # bound scanner snapshot, so readers can verify both the total
            # scan and the resulting pool change without opening two files.
            pool_symbols = sorted({
                symbol
                for groups_key in ('baseline', 'target_groups')
                for symbol_list in (bundle.get(groups_key) or {}).values()
                for symbol in symbol_list
            })
            names = {}
            if pool_symbols:
                with db.transaction() as c:
                    rows = c.execute(
                        'SELECT symbol,name FROM quant.instruments WHERE symbol = ANY(%s)',
                        (pool_symbols,),
                    ).fetchall()
                names = {row['symbol']: row['name'] for row in rows}
            write(a.directory / '推荐决策.md', markdown(bundle, scan, names))
            print(json.dumps({'decision_id': bundle['decision_id'], 'status': bundle['status'], 'coverage': bundle['coverage']}, ensure_ascii=False))
            if not bundle['sync_allowed']:
                return 2
            from app.recommendation_pool.followup import register_decision
            followup = register_decision(db, scan, bundle)
            write(a.directory / 'followup-registration.json', followup)
        else:
            if not a.snapshot:
                raise ValueError('fresh_ths_snapshot_required')
            if a.intraday_run_id:
                with db.transaction() as c:
                    row = c.execute('''SELECT result FROM quant.recommendation_pool_decisions
                        WHERE intraday_run_id=%s ORDER BY created_at DESC LIMIT 1''',
                        (a.intraday_run_id,)).fetchone()
                bundle = (row or {}).get('result') or {}
            else:
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
