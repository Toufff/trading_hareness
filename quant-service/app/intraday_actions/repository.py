"""Isolated SQLite shadow evidence. This is NOT the production event store.

No network, broker writes or schema migration. An explicit local path is required.
The production adapter must implement the same transactional contract before the
feature can be enabled. Delivery is not a fill and never releases cash by itself.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from typing import Any, Iterator


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class ShadowRepository:
    def __init__(self, path: Any):
        self.connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE IF NOT EXISTS action_states (
                account_key TEXT, symbol TEXT, plan_id TEXT, version TEXT, states TEXT NOT NULL,
                PRIMARY KEY(account_key,symbol,plan_id,version));
            CREATE TABLE IF NOT EXISTS action_outbox (
                event_key TEXT PRIMARY KEY, account_key TEXT NOT NULL, payload TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                reason TEXT, reserve_cash REAL NOT NULL, reservation_evidence TEXT);
            CREATE TABLE IF NOT EXISTS action_coverage (
                account_key TEXT, symbol TEXT, observed_at TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(account_key,symbol));
            CREATE TABLE IF NOT EXISTS compiled_plans (cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed_fills (account_key TEXT, fill_id TEXT,
                episode_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(account_key,fill_id));
            CREATE TABLE IF NOT EXISTS t_episodes (account_key TEXT, episode_id TEXT,
                payload TEXT NOT NULL, PRIMARY KEY(account_key,episode_id));
        ''')

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.execute('ROLLBACK')
            raise

    def states(self, account: str, symbol: str, plan_id: str, version: str) -> dict:
        row = self.connection.execute('SELECT states FROM action_states WHERE account_key=? AND symbol=? AND plan_id=? AND version=?',
                                      (account, symbol, plan_id, version)).fetchone()
        return json.loads(row['states']) if row else {}

    def save_states(self, account: str, symbol: str, plan_id: str, version: str, states: dict) -> None:
        self.connection.execute('INSERT INTO action_states VALUES(?,?,?,?,?) ON CONFLICT(account_key,symbol,plan_id,version) DO UPDATE SET states=excluded.states',
                                (account, symbol, plan_id, version, _json(states)))

    def reserved_cash(self, account: str) -> float:
        row = self.connection.execute('SELECT coalesce(sum(reserve_cash),0) AS cash FROM action_outbox WHERE account_key=? AND reservation_evidence IS NULL', (account,)).fetchone()
        return float(row['cash'])

    def enqueue(self, event: dict, *, expires_at: str, reserve_cash: float) -> bool:
        cursor = self.connection.execute('INSERT OR IGNORE INTO action_outbox(event_key,account_key,payload,expires_at,status,reserve_cash) VALUES(?,?,?,?,?,?)',
                                         (event['event_key'], event['account_key'], _json(event), expires_at, 'pending', reserve_cash))
        return cursor.rowcount == 1

    def save_coverage(self, account: str, symbol: str, observed_at: str, result: dict) -> None:
        self.connection.execute('INSERT INTO action_coverage VALUES(?,?,?,?) ON CONFLICT(account_key,symbol) DO UPDATE SET observed_at=excluded.observed_at,result=excluded.result',
                                (account, symbol, observed_at, _json(result)))

    def pending(self) -> list[dict]:
        return [{**dict(row), 'payload': json.loads(row['payload'])} for row in self.connection.execute(
            "SELECT event_key,account_key,payload,expires_at,status,attempts,reason,reserve_cash FROM action_outbox WHERE status='pending' ORDER BY rowid")]

    def claim(self, event_key: str) -> bool:
        return self.connection.execute("UPDATE action_outbox SET status='sending',attempts=attempts+1 WHERE event_key=? AND status='pending'", (event_key,)).rowcount == 1

    def is_pending(self, event_key: str) -> bool:
        row = self.connection.execute('SELECT status FROM action_outbox WHERE event_key=?', (event_key,)).fetchone()
        return bool(row and row['status'] == 'pending')

    def quarantine_interrupted_sends(self) -> int:
        """Call only after proving the previous sender process has terminated."""
        return self.connection.execute("UPDATE action_outbox SET status='uncertain',reason='sender_interrupted_needs_receipt_reconciliation' WHERE status='sending'").rowcount

    def cached_plan(self, key: str) -> dict | None:
        row = self.connection.execute('SELECT payload FROM compiled_plans WHERE cache_key=?', (key,)).fetchone()
        return json.loads(row['payload']) if row else None

    def cache_plan(self, key: str, payload: dict) -> dict:
        self.connection.execute('INSERT OR IGNORE INTO compiled_plans VALUES(?,?)', (key, _json(payload)))
        return self.cached_plan(key)

    def enrich_cached_plan(self, key: str, payload: dict) -> None:
        self.connection.execute('UPDATE compiled_plans SET payload=? WHERE cache_key=?', (_json(payload), key))

    def consume_fill(self, account: str, episode: str, fill: dict) -> bool:
        if not fill.get('fill_id') or fill.get('verified') is not True:
            raise ValueError('verified immutable fill required')
        existing = self.connection.execute('SELECT episode_id,payload FROM consumed_fills WHERE account_key=? AND fill_id=?', (account, fill['fill_id'])).fetchone()
        if existing:
            if existing['episode_id'] != episode or existing['payload'] != _json(fill):
                raise ValueError('fill already consumed or changed')
            return False
        self.connection.execute('INSERT INTO consumed_fills VALUES(?,?,?,?)', (account, fill['fill_id'], episode, _json(fill)))
        return True

    def episode(self, account: str, episode_id: str) -> dict | None:
        row = self.connection.execute('SELECT payload FROM t_episodes WHERE account_key=? AND episode_id=?', (account, episode_id)).fetchone()
        return json.loads(row['payload']) if row else None

    def save_episode(self, episode: dict) -> None:
        self.connection.execute('INSERT INTO t_episodes VALUES(?,?,?) ON CONFLICT(account_key,episode_id) DO UPDATE SET payload=excluded.payload',
                                (episode['account_key'], episode['id'], _json(episode)))

    def other_episode_fill_ids(self, account: str, episode_id: str) -> list[str]:
        return [row['fill_id'] for row in self.connection.execute('SELECT fill_id FROM consumed_fills WHERE account_key=? AND episode_id<>?', (account, episode_id))]

    def mark(self, event_key: str, status: str, reason: str | None = None) -> None:
        if status not in {'pending', 'sending', 'delivered', 'expired', 'invalidated', 'uncertain'}:
            raise ValueError('unsupported delivery state')
        self.connection.execute('UPDATE action_outbox SET status=?,reason=?,attempts=attempts+? WHERE event_key=?',
                                (status, reason, int(status == 'sending'), event_key))
        if status in {'expired', 'invalidated'}:
            self.resolve_reservation(event_key, reason=status, evidence_id=reason or status)

    def resolve_reservation(self, event_key: str, *, reason: str, evidence_id: str) -> None:
        if not reason or not evidence_id:
            raise ValueError('reservation release requires explicit evidence')
        self.connection.execute('UPDATE action_outbox SET reservation_evidence=? WHERE event_key=?',
                                (_json({'reason': reason, 'evidence_id': evidence_id}), event_key))

    def close(self) -> None:
        self.connection.close()
