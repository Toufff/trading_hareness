"""Post-close holdings sync + human-vs-agent report (one run per trading day, 15:00-15:40).

Flow: capture the broker window -> if it is not on 资金股份, click the left
navigation tree (at most twice, re-reading after every click) -> two independent
screenshot reads -> envelope -> broker-fact-sync validate / persist / readback ->
agent paper comparison report written under reports/agent-paper.
"""
import argparse
from datetime import datetime, time as dtime, timezone
import html
import json
import msvcrt
import shutil
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from dotenv import load_dotenv  # noqa: E402

SHANGHAI = ZoneInfo('Asia/Shanghai')
CAPTURE_PS1 = ROOT / 'scripts' / 'windows' / 'broker-window-capture.ps1'
FACT_SYNC = ROOT / 'scripts' / 'broker-fact-sync.py'
AGENT_ACCOUNTS = ('agent-claude-opus', 'agent-dsh')


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def powershell(*arguments, timeout=60):
    # pwsh, not Windows PowerShell 5: the capture script is BOM-less UTF-8 and compares a Chinese window title.
    shell = shutil.which('pwsh') or r'C:\Program Files\PowerShell\7\pwsh.exe'
    completed = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                                str(CAPTURE_PS1), *arguments], capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return completed


def fact_sync(action, args, *extra):
    completed = subprocess.run([sys.executable, str(FACT_SYNC), action, '--phase', 'scheduled_close',
                                '--account-key', args.account_key, '--env-file', str(args.env_file), *extra],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        result = {'status': 'failed', 'error_code': 'BROKER_FACT_SYNC_OUTPUT_INVALID', 'stderr': completed.stderr[-500:]}
    return completed.returncode, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(r'G:\StockPlatform\config\runtime.env'))
    parser.add_argument('--platform-root', type=Path, default=Path(r'G:\StockPlatform'))
    parser.add_argument('--account-key', default='citics-primary')
    parser.add_argument('--helper-cache', default=r'G:\StockPlatform\data\broker-capture\bin-close-sync')
    parser.add_argument('--report-only', action='store_true', help='skip the sync, only rebuild the comparison report')
    parser.add_argument('--dry-run', action='store_true',
                        help='capture/navigate/read/build the envelope into a scratch folder; no run, no database write')
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)

    from app.broker_close_sync import (MAX_CLICKS, Capture, CloseSyncError, ScreenshotReader, build_envelope,
                                       click_target, navigation_pane)
    from app.database import Database
    from app.event_research.trading_calendar import is_open

    log_path = args.platform_root / 'logs' / 'broker-close-sync.jsonl'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(entry):
        entry = {'recorded_at': datetime.now(timezone.utc).isoformat(), **entry}
        with log_path.open('a', encoding='utf-8') as stream:
            stream.write(_json(entry) + '\n')
        print(_json(entry), flush=True)

    now = datetime.now(SHANGHAI)
    if not is_open(now.date()):
        log({'event': 'skipped', 'reason': 'not_trading_day'})
        return 0
    if not args.report_only and not args.dry_run and not dtime(15, 0) <= now.time() <= dtime(15, 40):
        log({'event': 'skipped', 'reason': 'outside_close_window'})
        return 0
    lock_path = args.platform_root / 'run' / 'broker-close-sync.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+')
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        log({'event': 'skipped', 'reason': 'already_running'})
        return 0

    db = Database()
    try:
        with db.transaction() as connection:
            done = connection.execute(
                """SELECT snapshot_id,observed_at FROM quant.broker_portfolio_snapshots
                    WHERE account_key=%s AND verification='verified_exact'
                      AND (observed_at AT TIME ZONE 'Asia/Shanghai')::date=%s
                      AND (observed_at AT TIME ZONE 'Asia/Shanghai')::time>='15:00'
                    ORDER BY observed_at DESC LIMIT 1""", (args.account_key, now.date())).fetchone()
        sync = {'status': 'skipped', 'reason': 'post_close_snapshot_exists', 'snapshot_id': str(done['snapshot_id'])} if done else None
        if args.dry_run:
            result = run_sync(args, log, db, now, MAX_CLICKS, Capture, CloseSyncError, ScreenshotReader, build_envelope,
                              click_target, navigation_pane)
            return 0 if result['status'] == 'dry_run_ok' else 2
        if sync is None and not args.report_only:
            sync = run_sync(args, log, db, now, MAX_CLICKS, Capture, CloseSyncError, ScreenshotReader, build_envelope,
                            click_target, navigation_pane)
        write_report(args, db, now, sync or {'status': 'skipped', 'reason': 'report_only'}, log)
        return 0 if (sync or {}).get('status') in {'verified_exact', 'skipped', None} else 2
    finally:
        db.close()


def run_sync(args, log, db, now, MAX_CLICKS, Capture, CloseSyncError, ScreenshotReader, build_envelope, click_target,
             navigation_pane):
    if args.dry_run:
        with db.transaction() as connection:
            prior = connection.execute(
                "SELECT metadata FROM quant.broker_portfolio_snapshots WHERE account_key=%s ORDER BY observed_at DESC LIMIT 1",
                (args.account_key,)).fetchone()
        folder = args.platform_root / 'data' / 'broker-evidence' / ('dry-run-' + now.strftime('%Y%m%dT%H%M%S'))
        folder.mkdir(parents=True, exist_ok=True)
        started = {'status': 'started', 'run_id': 'dry-run', 'evidence_dir': str(folder),
                   'account_binding': (prior['metadata'] or {}).get('account_binding')}
    else:
        code, started = fact_sync('start', args)
    if started.get('status') != 'started':
        log({'event': 'sync_failed', 'stage': 'start', **started})
        return {'status': 'failed', 'stage': 'start', **started}
    run_id, folder = started['run_id'], Path(started['evidence_dir'])
    clicks = []
    try:
        listed = powershell('-Mode', 'list', '-HelperCacheRoot', args.helper_cache)
        windows = json.loads(listed.stdout or '[]')
        targets = [w for w in windows if w.get('title') == '网上股票交易系统5.0' and w.get('visible')
                   and w['rect']['width'] > 100 and w['rect']['height'] > 100]
        if len(targets) != 1:
            raise CloseSyncError('BROKER_WINDOW_NOT_FOUND', f'{len(targets)} visible trading windows')
        target = targets[0]
        reader = ScreenshotReader()

        def capture(name):
            path = folder / name
            result = powershell('-Mode', 'capture', '-Hwnd', str(target['hwnd']), '-OutputPath', str(path),
                                '-HelperCacheRoot', args.helper_cache)
            receipt_path = Path(str(path) + '.receipt.json')
            receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig')) if receipt_path.exists() else {}
            if result.returncode != 0 or receipt.get('result') != 'success':
                raise CloseSyncError('BROKER_CAPTURE_FAILED', (receipt.get('error') or result.stderr or '')[-300:])
            return Capture(str(path), receipt['fileHash'], str(receipt_path), receipt)

        shot = capture('page-0.png')
        read = reader.read(str(folder), 'page-0.png')
        while read.get('page') != 'funds_holdings':
            if read.get('page') in {'dialog', 'unreadable'}:
                raise CloseSyncError('BROKER_UI_BLOCKED', f"{read.get('page')}: {read.get('notes', '')[:200]}")
            if len(clicks) >= MAX_CLICKS:
                raise CloseSyncError('BROKER_NAV_CLICK_LIMIT', f'still on {read.get("page")} after {len(clicks)} clicks')
            pane = navigation_pane(json.loads(powershell('-Mode', 'list', '-HelperCacheRoot', args.helper_cache).stdout), target)
            label, x, y = click_target(read, pane)
            before = shot.sha256
            outcome = click(args, target, x, y, 'click')
            time.sleep(2)
            shot = capture(f'page-{len(clicks) + 1}.png')
            mode = 'click'
            if shot.sha256 == before:
                # Background messages were ignored: bring the window forward briefly, click, give focus back.
                outcome = click(args, target, x, y, 'click-foreground')
                time.sleep(2)
                shot = capture(f'page-{len(clicks) + 1}.png')
                mode = 'click-foreground'
            clicks.append({'label': label, 'x': x, 'y': y, 'mode': mode, 'result': outcome})
            log({'event': 'navigation_click', 'run_id': run_id, **clicks[-1]})
            read = reader.read(str(folder), Path(shot.path).name)
        # A second independent reader on the same final capture.
        second = reader.read(str(folder), Path(shot.path).name)
        final = folder / 'holdings.png'
        Path(shot.path).replace(final)
        Path(shot.receipt_path).replace(Path(str(final) + '.receipt.json'))
        shot = Capture(str(final), shot.sha256, str(final) + '.receipt.json', shot.receipt)
        (folder / 'reads.json').write_text(_json([read, second]), encoding='utf-8')
        with db.transaction() as connection:
            prior = connection.execute(
                """SELECT metadata FROM quant.broker_portfolio_snapshots WHERE account_key=%s AND verification='verified_exact'
                    ORDER BY observed_at DESC LIMIT 1""", (args.account_key,)).fetchone()
        envelope = build_envelope(
            run={'run_id': run_id, 'account_key': args.account_key, 'account_binding': started['account_binding']},
            reads=[read, second], capture=shot, prior_identity=(prior['metadata'] or {}).get('account_identity') or {},
            trade_date=now.date().isoformat(), clicks=clicks)
        envelope_path = folder / 'envelope.json'
        envelope_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding='utf-8')
        if args.dry_run:
            log({'event': 'dry_run_ok', 'evidence_dir': str(folder), 'clicks': clicks, 'account': envelope['account'],
                 'positions': [(p['symbol'], p['quantity'], p['sellable_quantity'], p['market_price']) for p in envelope['positions']]})
            return {'status': 'dry_run_ok', 'evidence_dir': str(folder)}
        code, validated = fact_sync('complete', args, '--run-id', run_id, '--envelope', str(envelope_path), '--validate-only')
        if validated.get('status') != 'validated':
            raise CloseSyncError(validated.get('error_code') or 'BROKER_VALIDATE_FAILED', _json(validated)[:300])
        code, persisted = fact_sync('complete', args, '--run-id', run_id, '--envelope', str(envelope_path))
        if persisted.get('status') != 'verified_exact':
            # complete already marked the run failed on a write-path error.
            log({'event': 'sync_failed', 'stage': 'persist', 'run_id': run_id, **persisted})
            return {'status': 'failed', 'stage': 'persist', 'run_id': run_id, **persisted}
        log({'event': 'sync_ok', 'run_id': run_id, 'clicks': len(clicks), 'snapshot_id': persisted.get('snapshot_id'),
             'total_asset': envelope['account']['total_asset'], 'positions': len(envelope['positions'])})
        return {**persisted, 'clicks': clicks, 'total_asset': envelope['account']['total_asset']}
    except Exception as error:  # every failure ends the audited run with its code
        code_name = getattr(error, 'code', type(error).__name__)
        if not args.dry_run:
            fact_sync('fail', args, '--run-id', run_id, '--error-code', code_name, '--message', str(error)[:500])
        log({'event': 'sync_failed', 'run_id': run_id, 'error_code': code_name, 'detail': str(error)[:500], 'clicks': clicks})
        return {'status': 'failed', 'run_id': run_id, 'error_code': code_name, 'detail': str(error)[:500],
                'evidence_dir': str(folder)}


def click(args, target, x, y, mode):
    result = powershell('-Mode', mode, '-Hwnd', str(target['hwnd']), '-X', str(x), '-Y', str(y),
                        '-HelperCacheRoot', args.helper_cache)
    if result.returncode != 0:
        from app.broker_close_sync import CloseSyncError
        raise CloseSyncError('BROKER_NAV_CLICK_FAILED', f'{mode}: {result.stderr[-300:]}')
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {'raw': result.stdout[-300:]}


def write_report(args, db, now, sync, log):
    from app.agent_paper.report import status
    rows = []
    with db.transaction() as connection:
        for key in AGENT_ACCOUNTS:
            try:
                report = status(connection, account_key=key, day=now.date(), decisions=0)
            except Exception as error:  # an account that does not exist yet is not fatal
                rows.append({'account_key': key, 'error': str(error)[:200]})
                continue
            daily = next((d for d in report['daily'] if d['trading_date'] == now.date().isoformat()), None)
            rows.append({'account_key': key, 'model': report.get('model'), 'initial_equity': report.get('initial_equity'),
                         'daily': daily, 'orders': report.get('orders'), 'positions': report.get('positions')})
    result = {'generated_at': datetime.now(SHANGHAI).isoformat(), 'trading_date': now.date().isoformat(),
              'human_sync': sync, 'agents': rows}
    folder = args.platform_root / 'reports' / 'agent-paper'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f'{now.date().isoformat()}.json').write_text(_json(result), encoding='utf-8')
    (folder / f'{now.date().isoformat()}.html').write_text(render(result), encoding='utf-8')
    log({'event': 'report_written', 'path': str(folder / f'{now.date().isoformat()}.html'), 'sync_status': sync.get('status')})


def render(result):
    e = lambda v: html.escape('—' if v is None else str(v))
    pct = lambda v: '—' if v is None else f'{v:+.2f}%'
    first = next((r['daily'] for r in result['agents'] if r.get('daily')), None) or {}
    lines = [f"<tr><td>你（实盘）</td><td>{e(first.get('human_equity'))}</td><td>{pct(first.get('human_return_pct'))}</td>"
             f"<td>{e(first.get('human_basis'))}</td></tr>"]
    for row in result['agents']:
        d = row.get('daily') or {}
        lines.append(f"<tr><td>{e(row['account_key'])}</td><td>{e(d.get('agent_equity'))}</td><td>{pct(d.get('agent_return_pct'))}</td>"
                     f"<td>{e(d.get('agent_price_basis') or row.get('error'))}</td></tr>")
    orders = ''.join(
        f"<tr><td>{e(r['account_key'])}</td><td>{e(str(o.get('placed_at'))[11:19])}</td><td>{e(o.get('side'))}</td>"
        f"<td>{e(o.get('symbol'))} {e(o.get('name'))}</td><td>{e(o.get('quantity'))}</td><td>{e(o.get('status'))}</td>"
        f"<td>{e(o.get('fill_price'))}</td></tr>" for r in result['agents'] for o in (r.get('orders') or []))
    sync = result['human_sync']
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>模拟盘对比 {e(result['trading_date'])}</title>
<style>:root{{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea}}@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mute:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,sans-serif;margin:0 auto;max-width:880px;padding:16px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left}}
small{{color:var(--mute)}}</style></head><body>
<h1>模拟盘对比 · {e(result['trading_date'])}</h1>
<p><small>生成于 {e(result['generated_at'])}；实盘同步：{e(sync.get('status'))} {e(sync.get('error_code') or sync.get('reason') or '')}</small></p>
<table><tr><th>账户</th><th>收盘权益</th><th>收益</th><th>口径</th></tr>{''.join(lines)}</table>
<h2>Agent 当日委托</h2><table><tr><th>账户</th><th>时间</th><th>方向</th><th>标的</th><th>数量</th><th>状态</th><th>成交价</th></tr>{orders}</table>
</body></html>"""


if __name__ == '__main__':
    raise SystemExit(main())
