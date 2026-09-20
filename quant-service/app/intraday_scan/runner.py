"""Live I/O orchestration; rules and replay have no provider dependency."""
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import logging
from . import engine, repository, reports, source
from .rules import digest, validate_minute_health


def run(database, output_root):
    from ..short_term_lanes.repository import load, verified_events
    from ..short_term_lanes.service import governed_selection
    from ..short_term_lanes.candidate_history import fetch
    from ..short_term_lanes.enrichment_queue import symbols
    now = datetime.now(ZoneInfo('Asia/Shanghai')); cutoff = source.session_cutoff(now)
    run_id = repository.begin(database, cutoff, engine.VERSION); stage = 'availability'
    directory = Path(output_root) / (cutoff.strftime('%Y%m%d-%H%M') + '-' + run_id[:8])
    try:
        cutoff = source.available_cutoff(now)
        with database.transaction() as c:
            c.execute('UPDATE quant.intraday_strategy_scans SET cutoff=%s WHERE run_id=%s', (cutoff, run_id))
        def progress(value):
            nonlocal stage
            stage = value
            with database.transaction() as c:
                c.execute('UPDATE quant.intraday_strategy_scans SET stage=%s,updated_at=now() WHERE run_id=%s', (stage, run_id))
            print(json.dumps(dict(run_id=run_id, stage=stage)), flush=True)
        progress('market_capture'); rows, health = source.capture(cutoff)
        progress('history'); history, sessions = load(database, cutoff.date() - timedelta(days=1))
        seeds, baseline = repository.seeds(database, cutoff.date(), cutoff)
        settings, governance = governed_selection(database)
        from ..event_research.pipeline import context as event_context
        news = event_context(database, datetime.now(now.tzinfo), refresh=True)
        data = dict(cutoff=cutoff.isoformat(), requested_at=now.isoformat(), rows=rows, health=health,
                    history=history, sessions=sessions, seeds=seeds, baseline=baseline,
                    settings=asdict(settings), governance_config=governance, events=verified_events(database, cutoff.date()),
                    minutes={}, previous_plans=repository.previous_plans(database, cutoff),
                    observed_at=datetime.now(now.tzinfo).isoformat(), event_research=news)
        progress('formal_prefilter'); preliminary = engine.formal(data)
        queue = symbols(history + rows, sessions[-10:] + [str(cutoff.date())], preliminary)
        manual = [r['symbol'] for r in seeds if r.get('manual_recommended') or r.get('display_rank') is not None]
        queue = list(dict.fromkeys(manual + queue))[:96]
        progress('strict_ohlc'); data['price_histories'], data['history_health'] = fetch(queue, cutoff.date())
        progress('forming_ohlc')
        from ..longhu_vendor_source import LonghuVendorSource
        from .ohlc import merge
        data['quote_snapshots'],data['quote_health']=LonghuVendorSource().watch_quotes(queue,max_symbols=96)
        data['ohlc_captured_at'] = datetime.now(now.tzinfo).isoformat()
        data['price_histories'],data['history_health']=merge(data['price_histories'],data['quote_snapshots'],
                                                          data['cutoff'],data['ohlc_captured_at'])
        data['observed_at'] = data['ohlc_captured_at']
        progress('formal_enriched'); preliminary = engine.formal(data)
        items = engine.candidates(data, preliminary)
        pools = [sorted([r for r in items if r['lane'] == key], key=lambda r: -(r.get('formal_rank') or 0)) for key in engine.LABELS]
        chosen = list(dict.fromkeys(manual))
        for i in range(max([len(p) for p in pools] + [0])):
            for pool in pools:
                if i < len(pool) and pool[i]['symbol'] not in chosen: chosen.append(pool[i]['symbol'])
            if len(chosen) >= 96: break
        chosen = chosen[:max(96, len(set(manual)))]
        progress('minutes'); data['minutes'], errors = source.fetch_minutes(chosen, cutoff)
        validate_minute_health(data['minutes'], errors)
        data['minute_health'] = dict(requested=len(chosen), received=len(data['minutes']), errors=errors)
        data['observed_at'] = datetime.now(now.tzinfo).isoformat(); data['implementation_hash'] = engine.implementation_hash()
        data = json.loads(json.dumps(data, default=str, ensure_ascii=False))
        progress('evaluate'); result = engine.build(data)
        if digest(result) != digest(engine.build(data)): raise ValueError('Nondeterministic replay')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'input.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        progress('reports'); paths = reports.write(result, directory)
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        progress('database_readback'); repository.save(database, run_id, data, result)
        from ..trade_thesis.scan_adapter import refresh_from_run
        try:
            thesis_receipt = refresh_from_run(database, run_id, 'intraday')
        except Exception as exc:  # advisory stage failure must not erase a valid persisted scan
            logging.getLogger(__name__).exception("intraday_trade_thesis_hook_failed run_id=%s", run_id)
            thesis_receipt = {
                "status": "failed", "source_run_id": run_id, "error_type": type(exc).__name__,
                "live_effect": "none", "decision_binding": False,
            }
            try:
                with database.transaction() as c:
                    c.execute(
                        "UPDATE quant.intraday_strategy_scans "
                        "SET stage='completed_thesis_partial',error=%s WHERE run_id=%s",
                        (json.dumps({"trade_thesis": type(exc).__name__}), run_id),
                    )
            except Exception:
                logging.getLogger(__name__).exception(
                    "intraday_trade_thesis_failure_receipt_persist_failed run_id=%s", run_id,
                )
        try:
            from ..trade_thesis.report import sections as thesis_sections
            thesis_path = directory / 'trade-thesis.md'
            thesis_path.write_text('\n'.join(thesis_sections(thesis_receipt)), encoding='utf-8')
            thesis_receipt['report_path'] = str(thesis_path)
        except Exception as exc:
            thesis_receipt['report_error'] = type(exc).__name__
            logging.getLogger(__name__).exception('intraday_thesis_report_failed run_id=%s', run_id)
        receipt = dict(run_id=run_id, cutoff=data['cutoff'], observed_at=data['observed_at'],
                       input_hash=digest(data), result_hash=digest(result), database_readback=True,
                       phase=result['phase'], market=result['market'], history_health=data['history_health'],
                       trade_thesis=thesis_receipt,
                       minute_health=data['minute_health'], reports=paths)
        if cutoff.hour == 15:
            # Separate outcome: a comparison error cannot erase the valid scan.
            from .reconcile import persist_day
            try: receipt['reconciliation'] = persist_day(database, run_id)
            except Exception as exc:
                receipt['reconciliation'] = dict(status='failed', error_type=type(exc).__name__)
                with database.transaction() as c:
                    c.execute("UPDATE quant.intraday_strategy_scans SET stage='completed_reconciliation_failed',error=%s WHERE run_id=%s",
                              (type(exc).__name__,run_id))
        (directory / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
        return receipt
    except Exception as exc:
        repository.fail(database, run_id, stage, exc)
        raise


def replay(path, output_root):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('implementation_hash') != engine.implementation_hash():
        raise ValueError('Replay requires the original archived implementation')
    result = engine.build(data)
    paths = reports.write(result, Path(output_root) / ('replay-' + digest(data)[:12]))
    return dict(replay=True, result_hash=digest(result), reports=paths)
