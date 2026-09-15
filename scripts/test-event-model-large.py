"""Explicit billed, uncached real-news acceptance; does not publish a delivery."""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from dotenv import load_dotenv
from app.database import Database
from app.event_research import repository
from app.event_research import analysis as analysis_module
from app.event_research.analysis import discover, research_sample, model_review
from app.event_research.source import collect


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--documents', type=int, default=120)
    p.add_argument('--deadline', type=int, default=540)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--inject-bad-citation', action='store_true', help='Acceptance only: corrupt one generated alias to exercise the real repair model; never publishes')
    args = p.parse_args()
    if not 61 <= args.documents <= 240:
        p.error('Large acceptance requires 61..240 real documents')
    load_dotenv('G:/StockPlatform/config/runtime.env', override=True)
    db = Database()
    start = monotonic()
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'test': 'large_uncached_real_news', 'publishes_scheduled_success': False,
              'fault_injection': args.inject_bad_citation}
    original_completion=analysis_module.completion
    if args.inject_bad_citation:
        calls=0
        def inject_once(*a,**kw):
            nonlocal calls
            value,usage,model=original_completion(*a,**kw)
            calls+=1
            if calls==1 and value.get('events'):
                value['events'][0]['evidence_ids']=['D999999']
            return value,usage,model
        analysis_module.completion=inject_once
    try:
        docs, source = collect()
        result['source_status'] = source
        if source['status'] != 'ok':
            raise RuntimeError('Fresh source incomplete')
        repository.store_documents(db, docs)
        docs = repository.documents(db, datetime.now(timezone.utc))
        leads = discover(docs, repository.instruments(db))
        sample = research_sample(docs, leads, limit=args.documents)
        result['input_documents'] = len(sample)
        result['input_characters'] = len(json.dumps(sample, ensure_ascii=False))
        result['categories'] = sorted({c for lead in leads if lead['document_id'] in
                                      {d['document_id'] for d in sample} for c in lead['categories']})
        if len(sample) != args.documents:
            raise RuntimeError('Insufficient real input documents')
        print(json.dumps({**result, 'phase':'model_request'}, ensure_ascii=False), flush=True)
        review, analysis = model_review(sample, deadline_seconds=args.deadline)
        result.update(analysis=analysis, review=review)
        if not review or not review.get('events') or analysis['status'] != 'completed':
            raise RuntimeError('No complete event analysis')
        if args.inject_bad_citation and analysis.get('repair_count')!=1:
            raise RuntimeError('Real repair was not exercised')
        result['cited_documents'] = len({i for e in review['events'] for i in e['evidence_ids']})
        result['fits_normal_model_deadline'] = analysis['elapsed_seconds'] <= 240
        result['passed'] = True
    except Exception as exc:
        result.update(error_type=type(exc).__name__, progress=getattr(exc, 'progress', {}))
        result['diagnostics']=getattr(exc,'diagnostics',{})
    finally:
        analysis_module.completion=original_completion
        result['elapsed_seconds'] = round(monotonic()-start, 1)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        db.close()
    summary={k:v for k,v in result.items() if k != 'review'}
    if 'analysis' in summary:
        summary['analysis']={k:v for k,v in summary['analysis'].items() if k!='input_document_ids'}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
