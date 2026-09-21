"""Assembly and human-facing export; all selection rules remain pure."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from .repository import load, verified_events
from .rules import screen, Settings, LANES, features, mainboard
from .flow_experiments import compare as compare_flow_experiments
from ..ranking_factors import load_profile
from .advanced_strategies import prefilter_symbols
from .candidate_history import fetch as fetch_candidate_history
from .reviews import load as load_reviews, persist as persist_reviews
from .selection import project
from .reports import make_bundle, write_bundle
from .enrichment_queue import symbols as enrichment_symbols


def governed_selection(database):
    from dataclasses import replace
    from ..strategy_governance.repository import resolve_active_config
    settings=configured_settings()
    try:
        active=resolve_active_config(database)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception('governance_config_read_failed')
        return settings, dict(status='unavailable',error_type=type(exc).__name__,
            note='治理配置读取失败：本轮使用部署基线，不应用实验配置；九策略继续独立运行。')
    if active and active.get('status','active') == 'active':
        settings=replace(settings,ranking_factors=load_profile(active['config']['ranking_factors'],{key for key,_,_ in LANES}))
    elif active:
        return settings, dict(status='stale_code',generation=active.get('generation'),
            note='已批准可选因子与当前代码版本不一致：本轮隔离该因子，使用部署基线；需重新验证，基础策略未停用。')
    return settings, dict(status='active' if active else 'baseline',generation=active.get('generation') if active else 0)


def governed_settings(database) -> Settings:
    return governed_selection(database)[0]


def configured_settings(profile=None, *, disabled=False) -> Settings:
    path = None if disabled else (profile or os.getenv('QUANT_SHORT_TERM_FACTOR_PROFILE'))
    return Settings(ranking_factors=load_profile(path, {k for k, _, _ in LANES}) if path else ())


def build(database: Any, day: date, *, history_fetcher=fetch_candidate_history, settings=None, tracking_write=True) -> dict:
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    # This is a next-session close report, not a simulation of entering at 15:00.
    # Capture once and persist; replays must use this exact information boundary.
    cutoff=(now if day==now.date() else datetime.combine(day,time.max if day<now.date() else time(15),tzinfo=now.tzinfo)).isoformat()
    if settings is None:
        settings, governance_config = governed_selection(database)
    else:
        governance_config = {'status':'explicit_run_settings'}
    from ..event_research.pipeline import context as event_context
    news = event_context(database, now, refresh=day==now.date())
    if day != now.date():
        news = event_context(database, cutoff)
    else:
        cutoff=datetime.now(now.tzinfo).isoformat()
    rows, sessions = load(database, day)
    reviews = load_reviews(database, day)
    event_map = verified_events(database, day)
    for symbol, review in reviews.items():
        if review.get("catalyst"):
            source = review["sources"][0]
            assessment = review.get("event_assessment") or {}
            event_map.setdefault(symbol, []).append({"verified": True, "benefit": review["catalyst"],
                "url": source["url"], "published_date": source["published_date"],
                "available_at": assessment.get("available_at") or source.get("available_at") or (source["published_date"] + "T23:59:59+08:00"),
                "event_type": assessment.get("event_type") or "company_catalyst",
                "source": "primary_review", "surprise": assessment.get("surprise", "unknown"),
                "priced_in": assessment.get("priced_in", "unknown"),
                "impact_direction": assessment.get("impact_direction", "unknown")})
    preliminary = screen(rows, sessions, str(day), events=event_map, settings=settings, information_cutoff=cutoff)
    history_symbols = enrichment_symbols(rows, sessions, preliminary)
    if history_symbols:
        price_histories, history_health = history_fetcher(history_symbols, day)
    else:
        price_histories, history_health = {}, {
            "requested": 0, "ready": 0, "failed": 0,
            "source": "longhuvip:GetKLineDay_W14", "note": "基础收盘截面不足，未启动候选OHLC补充",
        }
    result = screen(
        rows, sessions, str(day), events=event_map,
        price_histories=price_histories, history_health=history_health, settings=settings, information_cutoff=cutoff,
    )
    from ..strategy_governance.configuration import code_fingerprint
    result['strategy_code_hash']=code_fingerprint()
    from ..strategy_governance.semantic_identity import decision_versions
    result['decision_versions'] = decision_versions(result.get('settings', {}))
    result['event_research']=news
    from ..event_research.impact_audit import consumption
    result['news_consumption'] = consumption(result, news)
    result["company_reviews"] = list(reviews.values())
    result['flow_sensitivity'] = (compare_flow_experiments(rows, sessions, settings, features, mainboard)
        if result['status']=='completed' else {'status':'data_gap', 'production_effect':'none'})
    result['governance_config'] = governance_config
    result.update(project(result, result['company_reviews']))
    result["coverage"]["verified_event_symbols"] = len(event_map)
    for lane in result["lanes"]:
        for item in lane["selected"]+lane.get("caution_list", []):
            if item["symbol"] in reviews:
                item["company_review"] = reviews[item["symbol"]]
    from .tracking_repository import refresh as refresh_tracking
    try:
        result['followup'] = refresh_tracking(database,result,day,write=tracking_write)
    except Exception as exc:
        # Keep today's independent scan; clearly mark the separate failed ledger.
        import logging
        logging.getLogger(__name__).exception('observation_tracking_failed date=%s', day)
        result['followup'] = dict(status='failed',as_of_date=str(day),items=[],total=0,
            error_type=type(exc).__name__,note='历史跟踪失败，不能解释为没有旧候选；查看 observation_tracking_failed 日志。')
    if tracking_write:
        try:
            from ..strategy_governance.frozen_inputs import freeze_input
            result['governance_input'] = freeze_input(day=day,rows=rows,sessions=sessions,
                events=event_map,price_histories=price_histories,history_health=history_health,information_cutoff=cutoff)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('strategy_governance_input_archive_failed date=%s',day)
            result['governance_input'] = dict(status='failed',error_type=type(exc).__name__,
                reason='Experiment input archive failed; current scan remains available, no fabricated replacement input')
        try:
            from .governance_checks import inspect_run
            result['governance_check'] = inspect_run(database,result)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('strategy_governance_check_failed date=%s',day)
            result['governance_check'] = dict(status='failed',error_type=type(exc).__name__,live_effect='none')
    from ..effectiveness.pipeline import attach
    attach(database,result,day,write=tracking_write)
    result['report_bundle'] = make_bundle(result)
    return result


def render(result: dict) -> str:
    """Compatibility entry point for consumers of the former single report."""
    bundle = result.get('report_bundle') or make_bundle(result)
    return next(r['markdown'] for r in bundle['reports'] if r['key'] == 'overview')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--collect-only", action="store_true", help="Collect inputs only; the owner builds and persists the single publication generation")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--review-file", type=Path)
    factors = parser.add_mutually_exclusive_group()
    factors.add_argument('--factor-profile', type=Path, help='Opt-in JSON factor profile; omitted = runtime config or off')
    factors.add_argument('--no-factors', action='store_true', help='Explicit baseline, ignoring runtime factor configuration')
    args = parser.parse_args()
    settings = configured_settings(args.factor_profile, disabled=args.no_factors) if args.factor_profile or args.no_factors else None
    if settings and settings.ranking_factors and args.output_dir and args.output_dir.resolve() == Path('G:/StockPlatform/reports/short-term').resolve():
        parser.error('Ad-hoc factor runs require a separate output directory; do not overwrite the published baseline reports')
    from ..database import Database
    database = Database()
    if args.review_file:
        persist_reviews(database, args.date, json.loads(args.review_file.read_text(encoding="utf-8")))
    if args.collect or args.collect_only:
        from .collect import collect
        collect(database, args.date, log=lambda item: print(json.dumps(item, ensure_ascii=False), flush=True))
    if args.collect_only:
        print(json.dumps({'status': 'collected', 'as_of_date': str(args.date), 'scan_executed': False}))
        return
    result = build(database, args.date, settings=settings)
    if args.output_dir:
        write_bundle(args.output_dir, result, result['report_bundle'])
    print(json.dumps({"status": result["status"], "coverage": result["coverage"],
        "lanes": [{"name": r["label"], "matches": r["total_matches"],
                   "selected": [s["name"] for s in r["selected"]]} for r in result["lanes"]]}, ensure_ascii=False))
    if result["status"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
