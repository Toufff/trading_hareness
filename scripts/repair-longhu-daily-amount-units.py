"""Restore Longhu daily amounts quarantined by the lots/thousand-CNY unit guard.

The Longhu close merge used to pass the kline ``bal`` field (CNY) straight into
the canonical daily contract (thousand CNY), so from 2026-09-04 the unit guard
nulled every Longhu amount; the 2026-09-01 rows predate the guard and were kept
in CNY.  The immutable raw ``daily_bar`` observation still holds the vendor CNY
value.  This repair rescales it once, re-applies the same ratio guard, and
resolves the matching open quality issues.  Rows from 2026-09-01/04 were
merged from Tencent realtime quotes, whose STAR-market volume is shares; those
volumes are rescaled to lots as well.  Dry run unless ``--apply``.
"""
import argparse
import json
from pathlib import Path
import sys

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'quant-service'))
from app.daily_bar_repository import TUSHARE_DAILY_AMOUNT_RATIO_MAX, TUSHARE_DAILY_AMOUNT_RATIO_MIN
from app.db_dsn import connection_params

PROVIDER = 'longhuvip_composite'

REPAIRED = """
WITH raw AS (
    SELECT DISTINCT ON (o.symbol, (o.effective_at AT TIME ZONE 'Asia/Shanghai')::date)
           o.symbol, (o.effective_at AT TIME ZONE 'Asia/Shanghai')::date AS trading_date,
           nullif(o.payload->>'amount','')::numeric / 1000 AS amount_thousand_cny
      FROM quant.raw_market_observations o
     WHERE o.provider_key=%(provider)s AND o.capability='daily_bar'
       AND o.effective_at >= %(since)s::date
     ORDER BY o.symbol, (o.effective_at AT TIME ZONE 'Asia/Shanghai')::date, o.available_at DESC
), fixed AS (
    SELECT b.symbol, b.trading_date, b.amount AS old_amount, raw.amount_thousand_cny AS new_amount,
           b.volume AS old_volume,
           -- Before 2026-09-07 the close used Tencent realtime quotes, whose STAR volume is shares.
           CASE WHEN b.symbol ~ '^68[89]' AND raw.amount_thousand_cny / (b.volume * b.close) < %(ratio_min)s
                THEN b.volume / 100 ELSE b.volume END AS new_volume, b.close
      FROM quant.{table} b JOIN raw ON raw.symbol=b.symbol AND raw.trading_date=b.trading_date
     WHERE b.{provider_column}=%(provider)s AND b.trading_date >= %(since)s::date
       AND raw.amount_thousand_cny > 0 AND b.volume > 0 AND b.close > 0
)
SELECT * FROM fixed
 WHERE new_amount / (new_volume * close) BETWEEN %(ratio_min)s AND %(ratio_max)s
   AND (old_amount IS DISTINCT FROM new_amount OR old_volume <> new_volume)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='2026-09-01')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    cfg = dict(line.split('=', 1) for line in Path(r'G:\StockPlatform\config\runtime.env').read_text(encoding='utf-8-sig').splitlines()
               if '=' in line and not line.startswith('#'))
    params = {'provider': PROVIDER, 'since': args.since,
              'ratio_min': TUSHARE_DAILY_AMOUNT_RATIO_MIN, 'ratio_max': TUSHARE_DAILY_AMOUNT_RATIO_MAX}
    result = {'apply': args.apply, 'since': args.since}
    with psycopg.connect(**connection_params(cfg), row_factory=dict_row) as connection:
        for table, provider_column in (('market_bars_daily', 'source'), ('canonical_bars_daily', 'selected_provider')):
            query = REPAIRED.format(table=table, provider_column=provider_column)
            rows = connection.execute(query, params).fetchall()
            result[table] = {'rows': len(rows), 'was_null': sum(r['old_amount'] is None for r in rows),
                             'was_cny': sum(r['old_amount'] is not None for r in rows),
                             'volume_shares_to_lots': sum(r['old_volume'] != r['new_volume'] for r in rows)}
            if args.apply and rows:
                connection.execute(
                    f"""UPDATE quant.{table} b SET amount=t.amount, volume=t.volume
                          FROM unnest(%s::text[],%s::date[],%s::numeric[],%s::numeric[]) AS t(symbol,trading_date,amount,volume)
                         WHERE b.symbol=t.symbol AND b.trading_date=t.trading_date AND b.{provider_column}=%s""",
                    ([r['symbol'] for r in rows], [r['trading_date'] for r in rows], [r['new_amount'] for r in rows],
                     [r['new_volume'] for r in rows], PROVIDER),
                )
        resolvable = connection.execute(
            """SELECT count(*) AS n FROM quant.data_quality_issues i
                WHERE i.code='daily_amount_unit_mismatch' AND i.resolved_at IS NULL
                  AND i.details->>'provider'=%(provider)s AND i.trading_date >= %(since)s::date""", params).fetchone()['n']
        if args.apply:
            # Only issues whose canonical Longhu bar now carries a guard-passing amount.
            resolvable = connection.execute(
                """UPDATE quant.data_quality_issues i SET resolved_at=now(),
                          details=i.details || jsonb_build_object('resolution','longhu_cny_rescaled_to_thousand_cny')
                     FROM quant.market_bars_daily b
                    WHERE i.code='daily_amount_unit_mismatch' AND i.resolved_at IS NULL
                      AND i.details->>'provider'=%(provider)s AND i.trading_date >= %(since)s::date
                      AND b.symbol=i.symbol AND b.trading_date=i.trading_date AND b.source=%(provider)s
                      AND b.amount > 0 AND b.amount / (b.volume * b.close) BETWEEN %(ratio_min)s AND %(ratio_max)s""",
                params).rowcount
        result['quality_issues_resolved' if args.apply else 'quality_issues_open'] = resolvable
        if not args.apply:
            connection.rollback()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
