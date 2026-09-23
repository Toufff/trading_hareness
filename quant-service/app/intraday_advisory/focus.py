"""Manual intraday focus: explicit, held-stock-only, and same-session expiring."""

from __future__ import annotations

from datetime import datetime, time
import re
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from ..broker_snapshot_freshness import broker_freshness

SHANGHAI = ZoneInfo('Asia/Shanghai')
SYMBOL = re.compile(r'^\d{6}\.(?:SH|SZ|BJ)$')
ACCOUNT = re.compile(r'^[a-zA-Z0-9_-]{1,80}$')


def validate_identity(account_key: str, symbol: str | None = None) -> None:
    if not ACCOUNT.fullmatch(account_key):
        raise HTTPException(422, 'invalid account key')
    if symbol is not None and not SYMBOL.fullmatch(symbol):
        raise HTTPException(422, 'invalid stock symbol')


def session_expiry(now: datetime) -> datetime:
    local = now.astimezone(SHANGHAI)
    expiry = datetime.combine(local.date(), time(15, 5), tzinfo=SHANGHAI)
    if local >= expiry:
        raise HTTPException(409, 'today intraday focus window has ended')
    return expiry


async def list_focus(async_database: Any, account_key: str, now: datetime) -> dict[str, Any]:
    validate_identity(account_key)
    async with async_database.transaction() as connection:
        result = await connection.execute("""
            SELECT s.snapshot_id,s.observed_at,s.verification,s.metadata
              FROM quant.broker_portfolio_snapshots s
             WHERE s.account_key=%s AND s.verification='verified_exact'
             ORDER BY s.observed_at DESC,s.snapshot_id DESC LIMIT 1""", (account_key,))
        snapshot = await result.fetchone()
        if snapshot is None:
            return {'account_key': account_key, 'snapshot_at': None,
                    'snapshot_current': False, 'items': []}
        current = broker_freshness(dict(snapshot), now)['current']
        result = await connection.execute("""
            SELECT p.symbol,p.name,p.quantity,
                   f.intent,f.expires_at
              FROM quant.broker_position_snapshots p
              LEFT JOIN quant.intraday_holding_focus f
                ON f.account_key=%s AND f.symbol=p.symbol AND f.expires_at>%s
             WHERE p.snapshot_id=%s AND p.quantity>0
             ORDER BY p.market_value DESC NULLS LAST,p.symbol""",
            (account_key, now, snapshot['snapshot_id']))
        rows = [dict(row) for row in await result.fetchall()]
    return {'account_key': account_key, 'snapshot_at': snapshot['observed_at'],
            'snapshot_current': current,
            'items': [{**row, 'focused': row['intent'] is not None} for row in rows]}


async def set_focus(async_database: Any, account_key: str, symbol: str, now: datetime) -> dict[str, Any]:
    validate_identity(account_key, symbol)
    expiry = session_expiry(now)
    async with async_database.transaction() as connection:
        result = await connection.execute("""
            SELECT snapshot_id,observed_at,verification,metadata
              FROM quant.broker_portfolio_snapshots
             WHERE account_key=%s AND verification='verified_exact'
             ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1""", (account_key,))
        snapshot = await result.fetchone()
        if snapshot is None or not broker_freshness(dict(snapshot), now)['current']:
            raise HTTPException(409, 'current verified holdings snapshot is required')
        result = await connection.execute("""
            SELECT name FROM quant.broker_position_snapshots
             WHERE snapshot_id=%s AND symbol=%s AND quantity>0""",
            (snapshot['snapshot_id'], symbol))
        position = await result.fetchone()
        if position is None:
            raise HTTPException(409, 'symbol is not in the latest verified holdings snapshot')
        await connection.execute("""
            INSERT INTO quant.intraday_holding_focus(account_key,symbol,intent,expires_at)
            VALUES(%s,%s,'intraday_t',%s)
            ON CONFLICT(account_key,symbol) DO UPDATE SET
              intent=EXCLUDED.intent,expires_at=EXCLUDED.expires_at,updated_at=clock_timestamp()""",
            (account_key, symbol, expiry))
    return {'account_key': account_key, 'symbol': symbol, 'name': position['name'],
            'intent': 'intraday_t', 'focused': True, 'expires_at': expiry}


async def clear_focus(async_database: Any, account_key: str, symbol: str) -> dict[str, Any]:
    validate_identity(account_key, symbol)
    async with async_database.transaction() as connection:
        await connection.execute('DELETE FROM quant.intraday_holding_focus WHERE account_key=%s AND symbol=%s',
                                 (account_key, symbol))
    return {'account_key': account_key, 'symbol': symbol, 'focused': False}
