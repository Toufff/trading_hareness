"""Regenerate quant-service/app/trade_discipline/exposure_calibration.json from the database, read-only.

Every connection this script opens carries ``-c default_transaction_read_only=on``: PostgreSQL itself
refuses any write.  The only output is the JSON artifact (``--output``, default the repository copy).

    python scripts/calibrate-discipline-exposure.py --env-file G:/StockPlatform/config/runtime.env

Deterministic: the same database content gives byte-identical output (samples are aggregated into
multisets and every quantile is taken over the sorted multiset; no clock is written).
"""
import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))

COLUMNS = 'symbol,trading_date,open,high,low,close,pre_close,volume,amount,adj_factor,is_suspended,limit_down'
CHUNKS = 64


def _connect():
    import psycopg
    from psycopg.rows import dict_row
    from app.db_dsn import connection_params
    params = connection_params()
    if params['host'] == 'postgres':
        params['host'] = '127.0.0.1'
    return psycopg.connect(**params, options='-c default_transaction_read_only=on', row_factory=dict_row,
                           connect_timeout=15)


def _work(job):
    """One chunk of symbols: read their full history, return the loss multisets per cell."""
    symbols, current_st, resume_dates = job
    from app.trade_discipline import exposure_calibration as calibration
    resume = frozenset(resume_dates)
    stage_losses, holiday_losses, counts = {}, {}, {}
    with _connect() as connection:
        rows = connection.execute(
            f'SELECT {COLUMNS} FROM quant.canonical_bars_daily WHERE symbol = ANY(%s) ORDER BY symbol,trading_date',
            (symbols,)).fetchall()
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(row['symbol'], []).append(row)
    for symbol in sorted(by_symbol):
        samples, symbol_counts = calibration.symbol_samples(
            symbol, by_symbol[symbol], current_is_st=bool(current_st.get(symbol)), holiday_resume_dates=resume)
        for key, value in symbol_counts.items():
            counts[key] = counts.get(key, 0) + value
        for stage, board, loss, holiday in samples:
            stage_losses.setdefault((stage, board), []).append(loss)
            if holiday:
                holiday_losses.setdefault((stage, board), []).append(loss)
    return stage_losses, holiday_losses, counts


def main(argv=None):
    parser = argparse.ArgumentParser(description='纪律卡单票仓位上限校准（只读数据库，输出版本化 JSON）')
    parser.add_argument('--env-file', default='G:/StockPlatform/config/runtime.env')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'quant-service/app/trade_discipline/exposure_calibration.json')
    parser.add_argument('--workers', type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)))
    args = parser.parse_args(argv)
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        os.environ.pop(key, None)
    from dotenv import dotenv_values
    for key, value in dotenv_values(args.env_file).items():
        if value is not None and key.startswith('PG'):
            os.environ[key] = value

    from app.trade_discipline import exposure_calibration as calibration
    from app.trade_discipline.stage import STAGES

    with _connect() as connection:
        connection.execute('SET TRANSACTION READ ONLY')
        window = connection.execute(
            'SELECT min(trading_date) AS first, max(trading_date) AS last FROM quant.canonical_bars_daily').fetchone()
        market_sessions = [row['trading_date'] for row in connection.execute(
            'SELECT DISTINCT trading_date FROM quant.canonical_bars_daily WHERE NOT coalesce(is_suspended,false) '
            'ORDER BY trading_date').fetchall()]
        symbols = [row['symbol'] for row in connection.execute(
            'SELECT DISTINCT symbol FROM quant.canonical_bars_daily ORDER BY symbol').fetchall()]
        current_st = {row['symbol']: bool(row['is_st']) for row in connection.execute(
            'SELECT symbol,is_st FROM quant.instruments WHERE is_st IS NOT NULL').fetchall()}
    resume = sorted(calibration.holiday_resume_dates(market_sessions))
    jobs = [(symbols[index::CHUNKS], {symbol: current_st.get(symbol, False) for symbol in symbols[index::CHUNKS]},
             resume) for index in range(CHUNKS)]
    stage_losses, holiday_losses, counts = {}, {}, {}
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for part_stage, part_holiday, part_counts in pool.map(_work, jobs):
            for target, part in ((stage_losses, part_stage), (holiday_losses, part_holiday)):
                for key, losses in part.items():
                    target.setdefault(key, []).extend(losses)
            for key, value in part_counts.items():
                counts[key] = counts.get(key, 0) + value
    boards = sorted(calibration.BOARD_LABEL)
    stage_cells = calibration.build_cells(stage_losses, STAGES, boards)
    holiday_cells = calibration.build_cells(holiday_losses, STAGES, boards, holiday=True)
    diagnostics = {**{key: counts[key] for key in sorted(counts)}, 'symbols': len(symbols),
                   'holiday_resume_dates': [day.isoformat() for day in resume],
                   'holiday_samples': sum(len(losses) for losses in holiday_losses.values())}
    payload = calibration.artifact(stage_cells=stage_cells, holiday_cells=holiday_cells,
                                   first_date=window['first'].isoformat(), last_date=window['last'].isoformat(),
                                   diagnostics=diagnostics)
    newline = chr(10)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + newline,
                           encoding='utf-8', newline=newline)
    print(json.dumps({'output': str(args.output), 'version': payload['version'],
                      'samples': counts.get('samples'), 'holiday_samples': diagnostics['holiday_samples']},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
