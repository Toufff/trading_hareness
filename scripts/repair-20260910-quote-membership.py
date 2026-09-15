"""Repair only the falsely removed members from the recorded 2026-09-07 recovery.

The old quote writer closed members on missing OHLC; the new writer is
additive. No real listing/delisting dates or other sources are changed.
"""
import json
from pathlib import Path
import sys
import psycopg
from psycopg.rows import dict_row
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'quant-service'))
from app.db_dsn import connection_params

cfg = dict(l.split('=',1) for l in Path(r'G:\StockPlatform\config\runtime.env').read_text(encoding='utf-8-sig').splitlines() if '=' in l and not l.startswith('#'))
with psycopg.connect(**connection_params(cfg), row_factory=dict_row, options='-c statement_timeout=30000') as c:
    rows = c.execute("""UPDATE quant.universe_membership_history h
        SET effective_to=NULL,updated_at=now(),metadata=metadata ||
            '{"repair":"20260910_quote_missing_is_not_delisting"}'::jsonb
        WHERE universe_key='all_a' AND effective_to='2026-09-06'
          AND metadata->>'closed_by'='longhuvip_composite'
          AND NOT EXISTS(SELECT 1 FROM quant.universe_membership_history n
              WHERE n.universe_key=h.universe_key AND n.symbol=h.symbol AND n.effective_from>h.effective_from)
        RETURNING symbol""").fetchall()
    if len(rows)>300:
        raise RuntimeError('unexpected repair scope; transaction rolled back')
    enabled = c.execute("""UPDATE quant.universe_members SET enabled=true,updated_at=now(),
        metadata=metadata || '{"repair":"20260910_quote_missing_is_not_delisting"}'::jsonb
        WHERE universe_key='all_a' AND NOT enabled
          AND metadata->>'disabled_by_snapshot'='2026-09-07' RETURNING symbol""").fetchall()
    corrected = c.execute("""UPDATE quant.raw_market_observations
        SET capability='settled_quote',effective_at='2026-09-07 15:00:00+08',
            availability_basis='dated_licensed_close_crosscheck'
        WHERE provider_key='longhuvip_composite' AND capability='realtime_quote'
          AND payload->>'trade_date'='20260907'
          AND effective_at>='2026-09-10' RETURNING symbol""").fetchall()
    result={'restored_members':len(rows),'enabled_members':len(enabled),
            'corrected_historical_quote_timestamps':len(corrected)}
print(json.dumps(result))
