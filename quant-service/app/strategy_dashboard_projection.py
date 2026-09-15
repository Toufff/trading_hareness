"""Opt-in transport projection; preserve one copy of each distinct run summary.

The full endpoint remains the download/audit contract. This only removes exact
duplicates in the browser response; it never truncates candidates or DB history.
"""
from typing import Any


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
        else:
            result[key] = {field: value for field, value in run.items() if field != 'summary'}
            result[key]['summary_ref'] = reference + '.summary'
    result['response_view'] = 'dashboard'
    return result
