"""Opt-in transport projection; preserve one copy of each distinct run summary.

The full endpoint remains the download/audit contract. This only removes exact
duplicates in the browser response; it never truncates candidates or DB history.
"""
from typing import Any


def dashboard_summary_sql() -> str:
    """Discard bulky presentation/evaluation records inside PostgreSQL.

    Scanner-owned lanes deliberately remain intact until current_view has
    validated the recommendation hash. They are compacted AFTER that check.
    """
    return """(summary - 'trade_thesis' - 'trade_thesis_three_arm_snapshot'
        || jsonb_build_object('strategy_lanes',
          (coalesce(summary->'strategy_lanes','{}'::jsonb)
           - 'trade_thesis' - 'accumulation_observations' - 'accumulation_single_conditions'
           - 'followup' - 'effectiveness' - 'event_research')
          || jsonb_build_object('report_bundle',
             (coalesce(summary#>'{strategy_lanes,report_bundle}','{}'::jsonb) - 'reports')
             || jsonb_build_object('reports', coalesce((SELECT jsonb_agg(r - 'markdown')
                FROM jsonb_array_elements(coalesce(summary#>'{strategy_lanes,report_bundle,reports}','[]')) r), '[]'::jsonb))))) AS summary"""


def compact_summary(summary: dict[str, Any], run_id: Any) -> dict[str, Any]:
    if not isinstance(summary.get('strategy_lanes'), dict):
        return summary
    result = {k:v for k,v in summary.items() if k not in ('trade_thesis', 'trade_thesis_three_arm_snapshot')}
    scan = {k:v for k,v in summary['strategy_lanes'].items() if k not in (
        'followup','effectiveness','event_research','trade_thesis','accumulation_observations',
        'accumulation_single_conditions','review_groups','review_plan','company_reviews','sector_overview')}
    def pick(row):
        item = {k:v for k,v in row.items() if k not in ('metrics','events','company_review')}
        item['metrics'] = {k:v for k,v in (row.get('metrics') or {}).items()
                           if k in ('close','change_pct','amount','turnover')}
        return item
    scan['lanes'] = [{**{k:v for k,v in lane.items() if k not in (
        'tracking_candidates','selected','caution_list','observation_list')},
        'selected': [pick(p) for p in lane.get('selected', [])],
        'caution_list': [pick(p) for p in lane.get('caution_list', [])],
        'observation_count': len(lane.get('observation_list') or [])} for lane in scan.get('lanes', [])]
    bundle = scan.get('report_bundle') or {}
    scan['report_bundle'] = {**bundle, 'reports': [
        {k:v for k,v in report.items() if k not in ('markdown','review')}
        for report in bundle.get('reports', [])]}
    scan['detail_run_id'] = str(run_id)
    scan['details_deferred'] = True
    result['strategy_lanes'] = scan
    if isinstance(result.get('recommendation_pool'), dict):
        result['recommendation_pool'] = {k:v for k,v in result['recommendation_pool'].items() if k != 'screening'}
    return result


def dashboard_post_close(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    summaries: list[tuple[str, Any]] = []
    for key in ('run', 'latest_completed', 'latest_attempt', 'candidate_run'):
        run = payload.get(key)
        if not isinstance(run, dict) or 'summary' not in run:
            continue
        reference = next((name for name, summary in summaries if summary == run['summary']), None)
        if reference is None:
            summaries.append((key, run['summary']))
            result[key] = {**run, 'summary': compact_summary(run['summary'], run.get('run_id'))}
        else:
            result[key] = {field: value for field, value in run.items() if field != 'summary'}
            result[key]['summary_ref'] = reference + '.summary'
    result['response_view'] = 'dashboard'
    return result
