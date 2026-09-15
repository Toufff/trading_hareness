"""Real deployed readback acceptance. No mock and no provider/DB mutations."""
import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.short_term_lanes.report_audit import check_bundle
from app.short_term_lanes.publication import difference_paths


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-url',default='http://127.0.0.1:5681')
    parser.add_argument('--dashboard-url',default='http://127.0.0.1:5680')
    parser.add_argument('--date',required=True)
    parser.add_argument('--report-dir',type=Path,default=Path('G:/StockPlatform/reports/short-term'))
    parser.add_argument('--reports-only',action='store_true',help='Verify publication without claiming company research is complete')
    args=parser.parse_args()
    session=requests.Session();session.trust_env=False
    response=session.get(args.base_url+'/api/v1/strategy/post-close/latest',params={'as_of_date':args.date},timeout=30)
    response.raise_for_status();payload=response.json()
    result=payload['run']['summary']['strategy_lanes']
    saved=json.loads((args.report_dir/f'{args.date}_short_term_lanes.json').read_text(encoding='utf-8'))
    md=(args.report_dir/f'{args.date}_short_term_lanes.md').read_text(encoding='utf-8')
    checks={
        'date':payload['run']['as_of_date']==result['as_of_date']==args.date,
        'complete':result['status']=='completed',
        'separate_lanes':len(result['lanes'])==9 and len({l['key'] for l in result['lanes']})==9,
        'report_matches_api':result==saved,
        'eleven_sessions':len(result['sessions'])==11 and result['sessions'][-1]==args.date,
        'history_coverage':result['coverage']['complete_history']/result['coverage']['universe']>=.95,
        'no_buy_authorization':all(not r['buy_authorized'] for l in result['lanes'] for r in l['selected']),
        'rules_persisted':bool(result['settings']) and result['research_only'] is True,
        'market_regime_router':bool(result.get('market',{}).get('regime',{}).get('label'))
            and result['market']['regime'].get('probability_status')=='not_calibrated',
        'risk_layer_separate':result.get('risk_policy',{}).get('separate_from_alpha') is True
            and all(r.get('risk_envelope',{}).get('research_only') is True for l in result['lanes'] for r in l['selected']),
        'advanced_strategy_scope_explicit':all(
            l.get('status') in {'completed','data_gap'} and 'data_gaps' in l
            for l in result['lanes'] if l['key'] in {'contraction','rotation','reclaim'}),
    }
    rows=[r for l in result['lanes'] for r in l['selected']]
    checks['all_names_in_report']=all(f"{r['name']}（{r['symbol'].split('.')[0]}）" in md for r in rows)
    checks['all_conditions']=all(r['confirmation'] and r['invalidation'] and r['expiry'] for r in rows)
    # Recompute the money/turnover basis from dated observations, independent
    # of the ranking score or success status.
    checks['five_day_sum']=all(abs(sum(x['net'] for x in r['metrics']['flow_series'][-5:])-r['metrics']['net5'])<.02 for r in rows)
    checks['history_dates']=all([x['date'] for x in r['metrics']['flow_series']]==result['sessions'] for r in rows)
    response=session.get(args.dashboard_url+'/api/research/strategy/post-close/latest',params={'as_of_date':args.date},timeout=30)
    response.raise_for_status()
    dashboard_payload=response.json()
    checks['dashboard_projection_matches']=dashboard_payload['run']['summary']['strategy_lanes']==result
    plan=result.get('review_plan',[])
    groups={g['key']:g['items'] for g in result.get('review_groups',[])}
    expected=list(dict.fromkeys(l['selected'][0]['symbol'] for l in result['lanes'] if l['selected']))
    checks['scan_first_research_plan']=[r['symbol'] for r in plan]==expected
    checks['review_plan_traceable']=all(r['selection_reason'] and r['memberships'] and r['evidence_sha256'] and r['as_of_date']==args.date for r in plan)
    priority=groups.get('priority',[])
    reviewed={r['symbol'] for key,items in groups.items() if key!='background' for r in items}
    checks['representative_reviews_complete']=set(expected)<=set(reviewed)
    checks['priority_reasons_and_questions_visible']=all(
        any(r['symbol']==symbol and r['selection_reason'] in md and r['selection']['question'] in md and r['outcome_label'] in md for r in priority)
        for symbol in expected)
    checks['reviews_not_promoted_by_availability']=all(r['selection']['origin']=='scan' for r in priority)
    all_reviews=[r['symbol'] for group in groups.values() for r in group]
    checks['review_groups_disjoint']=len(all_reviews)==len(set(all_reviews))
    checks['review_before_raw_lists']=0<=md.find('## 本次结论')<md.find('## 策略对照与独立报告') and all(
        (next((r['conclusion'] for r in priority if r['symbol']==symbol), '')
         in md[:md.find('## 策略对照与独立报告')]) for symbol in expected)
    recommendation=payload['run']['summary'].get('recommendation_pool') or result.get('recommendation_pool') or {}
    recommendation_coverage=recommendation.get('coverage') or {}
    dashboard_recommendation=dashboard_payload['run']['summary'].get('recommendation_pool') or {}
    checks['formal_recommendation_ready']=recommendation.get('status')=='ready' and recommendation.get('sync_allowed') is True
    checks['formal_recommendation_coverage_complete']=(
        not recommendation_coverage.get('missing') and not recommendation_coverage.get('errors')
        and recommendation_coverage.get('reviewed',0)>=recommendation_coverage.get('required',0)
    )
    checks['formal_recommendation_research_only']=recommendation.get('research_only') is True and all(
        item.get('buy_authorized') is False for item in recommendation.get('reviewed',[]))
    checks['formal_recommendation_actions_complete']=all(
        item.get('trigger') and item.get('invalidation') and item.get('sources')
        for item in recommendation.get('recommended',[]))
    checks['dashboard_recommendation_matches']=(
        bool(recommendation.get('decision_id'))
        and dashboard_recommendation.get('decision_id')==recommendation.get('decision_id'))
    checks.update(check_bundle(result,args.report_dir))
    if args.reports_only:
        publication={'date','report_matches_api','dashboard_projection_matches','separate_lanes'}
        checks={k:v for k,v in checks.items() if k in publication or k.startswith('bundle_')}
    receipt={'passed':all(checks.values()),'as_of_date':args.date,'run_id':payload['run']['run_id'],
             'report_difference_paths':difference_paths(result,saved),
             'checks':checks,'selected_rows':len(rows),'unique_selected':len({r['symbol'] for r in rows}),
             'scope':'report publication only; not research completeness' if args.reports_only else 'actual provider-sourced report vs deployed owner API and dashboard API; not profitability validation'}
    print(json.dumps(receipt,ensure_ascii=False,indent=2))
    suffix='report_publication' if args.reports_only else 'short_term_acceptance'
    (args.report_dir/f'{args.date}_{suffix}.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    raise SystemExit(0 if receipt['passed'] else 1)


if __name__=='__main__':main()
