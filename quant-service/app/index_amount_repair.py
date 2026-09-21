"""Bounded, evidence-backed correction of wrongly quarantined index amounts.

No fetch, rescaling, OHLC/availability rewrite, or blanket issue dismissal.
Caller owns the transaction. Dry-run is the default.
"""
from decimal import Decimal
from typing import Any
from psycopg.types.json import Json
from .daily_asset_semantics import close_conflicts
from .daily_bar_repository import daily_amount_unit_mismatch


def repair_index_amounts(connection: Any, *, start, end, apply: bool = False) -> dict:
    rows = connection.execute("""
        SELECT b.symbol,b.trading_date,b.close,b.selected_provider,b.amount,
               o.observation_id,o.normalized
          FROM quant.canonical_bars_daily b
          JOIN LATERAL (
             SELECT observation_id,normalized FROM quant.raw_market_observations
              WHERE observation_id=ANY(b.source_observation_ids) AND provider_key=b.selected_provider
              ORDER BY available_at DESC,created_at DESC LIMIT 1
          ) o ON true
         WHERE b.trading_date BETWEEN %s AND %s AND b.amount IS NULL
           AND (b.symbol ~ '^000[0-9]{3}\\.SH$' OR b.symbol ~ '^399[0-9]{3}\\.SZ$')
           AND EXISTS (SELECT 1 FROM quant.data_quality_issues q
                 WHERE q.symbol=b.symbol AND q.trading_date=b.trading_date
                   AND q.code='daily_amount_unit_mismatch' AND q.resolved_at IS NULL)
         ORDER BY b.symbol,b.trading_date""", (start, end)).fetchall()
    eligible, rejected = [], []
    for row in rows:
        raw = row['normalized'] or {}
        try:
            amount = Decimal(str(raw['amount']))
            volume = Decimal(str(raw['volume'])) if raw.get('volume') is not None else None
            close = Decimal(str(raw['close']))
            if (not close.is_finite() or close <= 0 or amount <= 0
                    or close_conflicts(row['symbol'], Decimal(row['close']), close)
                    or daily_amount_unit_mismatch(symbol=row['symbol'], source=row['selected_provider'],
                                                  amount=amount, volume=volume, close=close)):
                raise ValueError('source_contract_or_close_mismatch')
        except (ValueError, TypeError, KeyError, ArithmeticError):
            rejected.append({'symbol': row['symbol'], 'date': str(row['trading_date']),
                             'reason': 'evidence_not_sufficient'})
            continue
        evidence = {'repair': 'index_amount_contract_v1', 'observation_id': str(row['observation_id']),
                    'provider': row['selected_provider'], 'restored_amount_thousand_cny': str(amount)}
        eligible.append({'symbol': row['symbol'], 'date': str(row['trading_date']), **evidence})
        if not apply:
            continue
        updated = connection.execute("""UPDATE quant.canonical_bars_daily SET amount=%s,canonicalized_at=now()
            WHERE symbol=%s AND trading_date=%s AND amount IS NULL AND selected_provider=%s
              AND %s::uuid=ANY(source_observation_ids) RETURNING symbol""",
            (amount, row['symbol'], row['trading_date'], row['selected_provider'], row['observation_id'])).fetchone()
        if not updated:
            raise RuntimeError('canonical evidence changed during repair; retry from a fresh plan')
        connection.execute("""UPDATE quant.market_bars_daily SET amount=%s
            WHERE symbol=%s AND trading_date=%s AND source=%s AND amount IS NULL""",
            (amount, row['symbol'], row['trading_date'], row['selected_provider']))
        connection.execute("""UPDATE quant.data_quality_issues SET resolved_at=now(),
            details=coalesce(details,'{}'::jsonb) || %s::jsonb
            WHERE symbol=%s AND trading_date=%s AND code='daily_amount_unit_mismatch' AND resolved_at IS NULL""",
            (Json(evidence), row['symbol'], row['trading_date']))
        connection.execute("""UPDATE quant.canonical_bars_daily b SET quality_status='fresh'
            WHERE symbol=%s AND trading_date=%s AND quality_status='partial'
              AND NOT EXISTS (SELECT 1 FROM quant.data_quality_issues q WHERE q.symbol=b.symbol
                 AND q.trading_date=b.trading_date AND q.resolved_at IS NULL)""",
            (row['symbol'], row['trading_date']))
    return {'mode': 'apply' if apply else 'plan', 'eligible': len(eligible),
            'rejected': rejected, 'corrections': eligible}
