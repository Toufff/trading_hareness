"""Bounded lock-wait retry for every ``quant.instruments`` writer.

Why this exists
---------------
Sorting (``ORDER BY 1``, see ``instrument_registry``) gives every writer in
this repository one shared lock order, and between two sorted writers that is
enough.  It does not protect the owner against a writer it does not control:
the peer's image still registers symbols one row at a time in payload order
(a single-row ``VALUES(...) ON CONFLICT(symbol) DO NOTHING`` registration,
the statement in every 2026-09-18 deadlock report).  A per-row
writer in random order can form a cycle with ANY other writer of an
overlapping symbol set, sorted or not.  The throwaway-cluster simulation of
2026-09-19 (owner on the sorted code, peer on its old per-row code -- i.e.
production that day) measured 101 deadlocks in 30 rounds and the owner's
post-close transaction surviving only 11 of 30.

So the owner stops being a party to such a cycle for longer than it takes to
notice one:

1. Each instrument statement runs under a ``SAVEPOINT`` with
   ``SET LOCAL lock_timeout`` set **below** ``deadlock_timeout`` (default
   300 ms against PostgreSQL's default 1 s).  PostgreSQL runs the deadlock
   check only in a backend that has itself waited ``deadlock_timeout``, and
   that backend is the one it aborts.  An owner wait that never reaches
   ``deadlock_timeout`` never runs the check, so the owner is not the victim;
   the other side of a genuine cycle is.
2. On ``lock_not_available`` (``55P03``) -- or ``deadlock_detected``
   (``40P01``), which can still reach the owner if an operator raises the
   timeout to or above ``deadlock_timeout`` -- the helper rolls back to the
   savepoint.  That releases every row lock and speculative-insert token the
   statement had taken (they belong to the aborted subtransaction), which is
   what breaks the cycle, and leaves the caller's outer transaction usable.
3. It sleeps a jittered, growing backoff and re-runs the same idempotent
   statement, up to a bounded number of attempts and a bounded total time,
   then raises ``InstrumentLockRetryExhausted``.  No other error is retried.
4. It restores the ``lock_timeout`` that was in force before the call.  A
   ``SET LOCAL`` made inside a savepoint that is RELEASEd stays in force until
   the end of the OUTER transaction, which would silently shorten every later
   lock wait of the caller; the prior value is therefore put back explicitly,
   inside the savepoint, before it is released.

What it does not do: a savepoint rollback only releases the locks taken by
THIS statement.  A caller that already holds ``quant.instruments`` row locks
from an earlier statement of the same transaction keeps them while it backs
off, so a cycle through those older locks is not broken by the retry (the
peer, waiting on them, will still be the one PostgreSQL aborts, because only
the peer's wait reaches ``deadlock_timeout``).  That is why the owner's
post-close path takes all of its instrument locks in its first, sorted,
single statement (``persist_stock_basic_instruments``) and why the per-row
``upsert_daily_bar`` loops still sort their payload.

Bounds and why
--------------
* ``lock_timeout`` 300 ms (``QUANT_INSTRUMENT_LOCK_TIMEOUT_MS``).  Well under
  the 1 s ``deadlock_timeout`` of the owner cluster, so the owner never runs
  the deadlock check; long enough that an ordinary short row lock (a sibling
  writer's sub-second statement) is simply waited out on the first attempt.
  The value is clamped to half of the server's actual ``deadlock_timeout``
  if configured at or above it, because at that point the mechanism no
  longer works.
* Backoff: full jitter over ``min(2 s, 0.2 s * 2**n)``, at least 50 ms, so
  two owner writers that collided do not retry in lock step and the peer's
  per-row transaction gets room to commit.
* At most 60 attempts and 60 s in total (``QUANT_INSTRUMENT_LOCK_MAX_ATTEMPTS``
  / ``QUANT_INSTRUMENT_LOCK_BUDGET_SECONDS``).  Today every service
  connection already carries ``lock_timeout=5s`` (``QUANT_DB_LOCK_TIMEOUT_MS``)
  so a plain wait gives up after 5 s; 60 s is twelve times more patient than
  that for a peer transaction that is merely slow, while staying far inside
  the 300 s statement budget of the long post-close transaction and never
  leaving the session idle-in-transaction for more than one backoff (<= 2 s)
  at a time.  A peer transaction that holds an overlapping row for longer
  than a minute is an incident, and a clear error naming the writer is the
  better outcome than an unbounded wait.

Diagnosability: every retry and every exhaustion is logged on
``app.instrument_lock_retry`` with the writer name, attempt, SQLSTATE, the
elapsed time and -- on the first retry and on exhaustion -- the sessions
currently holding a write lock on ``quant.instruments`` (pid, application
name, transaction age, statement head).  Process-wide counters are exposed by
``instrument_lock_counters()``.

This module depends only on the standard library and psycopg so that the
operator scripts under ``scripts/`` can import it with nothing but
``quant-service`` on ``sys.path``.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import psycopg
from psycopg import errors as pg_errors

logger = logging.getLogger("app.instrument_lock_retry")

#: The only two outcomes that are retried.  Everything else propagates on the
#: first occurrence -- a constraint violation or a statement timeout is not a
#: lock-order problem and must not be papered over by re-running it.
RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01"})
_RETRYABLE_ERRORS = (pg_errors.LockNotAvailable, pg_errors.DeadlockDetected)

DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS = 300
DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS = 60
DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS = 60.0
BACKOFF_BASE_SECONDS = 0.2
BACKOFF_CAP_SECONDS = 2.0
BACKOFF_FLOOR_SECONDS = 0.05

#: Read once per call, before the first savepoint: the value to restore and
#: the ceiling the per-statement timeout has to stay under.  ``pg_settings``
#: reports ``deadlock_timeout`` in milliseconds whatever unit it was set in.
_SETTINGS_SQL = (
    "SELECT current_setting('lock_timeout') AS lock_timeout,"
    "(SELECT setting::integer FROM pg_settings WHERE name='deadlock_timeout') AS deadlock_timeout_ms"
)
_SET_LOCK_TIMEOUT_SQL = "SELECT set_config('lock_timeout', %s, true)"

#: Who holds a write lock on the table right now.  Best effort, diagnostic
#: only: run in its own savepoint so that a failure here can never abort the
#: caller's transaction.
_HOLDERS_SQL = (
    "SELECT a.pid, a.application_name, "
    "round(extract(epoch FROM now()-a.xact_start)::numeric, 1)::float AS xact_age_s, "
    "a.wait_event_type, left(regexp_replace(a.query, '\\s+', ' ', 'g'), 160) AS query "
    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
    "WHERE l.locktype='relation' AND l.relation='quant.instruments'::regclass "
    "AND l.mode='RowExclusiveLock' AND l.granted AND l.pid<>pg_backend_pid() "
    "ORDER BY a.xact_start LIMIT 10"
)


def _bounded(environ: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        return min(maximum, max(minimum, float(environ.get(name, default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class InstrumentLockPolicy:
    """The bounds of one writer's retry loop.  See the module docstring."""

    lock_timeout_ms: int = DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS
    max_attempts: int = DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS
    budget_seconds: float = DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS
    backoff_base_seconds: float = BACKOFF_BASE_SECONDS
    backoff_cap_seconds: float = BACKOFF_CAP_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "InstrumentLockPolicy":
        env = os.environ if environ is None else environ
        return cls(
            lock_timeout_ms=int(_bounded(env, "QUANT_INSTRUMENT_LOCK_TIMEOUT_MS",
                                         DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS, 10, 60_000)),
            max_attempts=int(_bounded(env, "QUANT_INSTRUMENT_LOCK_MAX_ATTEMPTS",
                                      DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS, 1, 1_000)),
            budget_seconds=_bounded(env, "QUANT_INSTRUMENT_LOCK_BUDGET_SECONDS",
                                    DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS, 1, 600),
        )

    def effective_lock_timeout_ms(self, deadlock_timeout_ms: int | None) -> int:
        """The per-attempt timeout, forced strictly below ``deadlock_timeout``."""
        timeout = max(1, int(self.lock_timeout_ms))
        if deadlock_timeout_ms and deadlock_timeout_ms > 0 and timeout >= deadlock_timeout_ms:
            return max(1, int(deadlock_timeout_ms) // 2)
        return timeout

    def backoff_seconds(self, retry_number: int, rng: Callable[[float, float], float]) -> float:
        """Full jitter over an exponentially growing, capped window."""
        window = min(self.backoff_cap_seconds, self.backoff_base_seconds * (2 ** max(0, retry_number - 1)))
        return max(BACKOFF_FLOOR_SECONDS, rng(0.0, window))


class InstrumentLockRetryExhausted(psycopg.OperationalError):
    """A ``quant.instruments`` writer could not get its row locks in budget.

    Raised with the savepoint already rolled back, so the caller's
    transaction is still usable; the caller decides whether to abort it.  A
    ``psycopg.OperationalError`` so that existing "database failed" handling
    classifies it as one, with the last lock error chained as ``__cause__``.
    """

    def __init__(self, writer: str, attempts: int, elapsed_seconds: float, last_sqlstate: str | None) -> None:
        self.writer = writer
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.last_sqlstate = last_sqlstate
        super().__init__(
            f"quant.instruments writer {writer!r} gave up after {attempts} lock-wait attempts "
            f"in {elapsed_seconds:.1f}s (last SQLSTATE {last_sqlstate}); another transaction kept "
            f"overlapping instrument rows locked -- see the app.instrument_lock_retry log for the holders"
        )


_COUNTERS_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = {
    "calls": 0, "retries": 0, "lock_timeouts": 0, "deadlocks": 0, "exhausted": 0, "backoff_seconds": 0.0,
}


def _count(**deltas: float) -> None:
    with _COUNTERS_LOCK:
        for key, value in deltas.items():
            _COUNTERS[key] = _COUNTERS.get(key, 0) + value


def instrument_lock_counters() -> dict[str, float]:
    """Process-wide totals since start: calls, retries, lock_timeouts, deadlocks, exhausted, backoff_seconds."""
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def _server_connection(target: Any) -> psycopg.Connection | None:
    """The psycopg ``Connection`` behind ``target`` (a Connection or a Cursor).

    ``None`` for anything else -- the recording fakes the unit tests drive
    writers with.  Such an object holds no server lock that could be waited
    on, so there is nothing to retry: its statement is executed once, as it
    was before this module existed.  Every production path hands in a real
    psycopg connection (the pool's, ``psycopg.connect``'s) or a cursor on
    one, and ``tests/test_instrument_lock_retry.py`` pins that both of those
    take the retry path.
    """
    if isinstance(target, psycopg.Connection):
        return target
    if isinstance(target, psycopg.Cursor) and isinstance(target.connection, psycopg.Connection):
        return target.connection
    return None


def _column(row: Any, name: str, index: int) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(name)
    return row[index]


def _lock_holders(target: Any, connection: Any) -> list[dict[str, Any]] | str:
    try:
        with connection.transaction():
            rows = target.execute(_HOLDERS_SQL).fetchall()
    except Exception as error:  # noqa: BLE001 - diagnostics must never fail the write
        return f"unavailable: {type(error).__name__}"
    keys = ("pid", "application_name", "xact_age_s", "wait_event_type", "query")
    return [
        {key: _column(row, key, index) for index, key in enumerate(keys)}
        for row in (rows or [])
    ]


def execute_instrument_write(
    target: Any,
    query: str,
    params: Any = None,
    *,
    writer: str,
    policy: InstrumentLockPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[float, float], float] = random.uniform,
) -> int:
    """Run one ``quant.instruments`` write with a bounded lock-wait retry.

    ``target`` is the caller's psycopg ``Connection`` (or a ``Cursor`` on it),
    normally already inside the caller's transaction; the write joins that
    transaction through a savepoint.  ``query`` must be idempotent -- every
    instrument writer is an ``INSERT ... ON CONFLICT`` -- because it may run
    more than once.  ``writer`` names the call site in logs and errors.

    Returns the statement's ``rowcount``.
    """
    connection = _server_connection(target)
    if connection is None:
        cursor = target.execute(query, params)
        return getattr(cursor, "rowcount", -1)
    policy = policy or InstrumentLockPolicy.from_env()
    settings = target.execute(_SETTINGS_SQL).fetchone()
    prior_lock_timeout = _column(settings, "lock_timeout", 0)
    if prior_lock_timeout is None:
        prior_lock_timeout = "0"
    deadlock_timeout_ms = _column(settings, "deadlock_timeout_ms", 1)
    timeout_ms = policy.effective_lock_timeout_ms(int(deadlock_timeout_ms) if deadlock_timeout_ms else None)
    if timeout_ms != policy.lock_timeout_ms:
        logger.warning("instrument_lock_timeout_clamped", extra={
            "writer": writer, "configured_ms": policy.lock_timeout_ms,
            "deadlock_timeout_ms": deadlock_timeout_ms, "effective_ms": timeout_ms,
        })
    _count(calls=1)
    started = clock()
    attempt = 0
    while True:
        attempt += 1
        try:
            with connection.transaction():
                target.execute(_SET_LOCK_TIMEOUT_SQL, (f"{timeout_ms}ms",))
                cursor = target.execute(query, params)
                rowcount = getattr(cursor, "rowcount", -1)
                target.execute(_SET_LOCK_TIMEOUT_SQL, (str(prior_lock_timeout),))
        except _RETRYABLE_ERRORS as error:
            # The savepoint is already rolled back here: the statement's row
            # locks are released and SET LOCAL is undone with it.
            sqlstate = getattr(error, "sqlstate", None)
            elapsed = clock() - started
            _count(lock_timeouts=1 if sqlstate == "55P03" else 0, deadlocks=1 if sqlstate == "40P01" else 0)
            if attempt >= policy.max_attempts or elapsed >= policy.budget_seconds:
                _count(exhausted=1)
                logger.error("instrument_lock_retry_exhausted", extra={
                    "writer": writer, "attempts": attempt, "elapsed_s": round(elapsed, 3),
                    "sqlstate": sqlstate, "lock_timeout_ms": timeout_ms,
                    "holders": _lock_holders(target, connection),
                })
                raise InstrumentLockRetryExhausted(writer, attempt, elapsed, sqlstate) from error
            delay = min(policy.backoff_seconds(attempt, rng), max(0.0, policy.budget_seconds - elapsed))
            _count(retries=1, backoff_seconds=delay)
            logger.warning("instrument_lock_retry", extra={
                "writer": writer, "attempt": attempt, "sqlstate": sqlstate,
                "elapsed_s": round(elapsed, 3), "backoff_s": round(delay, 3), "lock_timeout_ms": timeout_ms,
                **({"holders": _lock_holders(target, connection)} if attempt == 1 else {}),
            })
            sleep(delay)
            continue
        if attempt > 1:
            logger.info("instrument_lock_retry_succeeded", extra={
                "writer": writer, "attempts": attempt, "elapsed_s": round(clock() - started, 3),
            })
        return rowcount


__all__ = [
    "DEFAULT_INSTRUMENT_LOCK_BUDGET_SECONDS",
    "DEFAULT_INSTRUMENT_LOCK_MAX_ATTEMPTS",
    "DEFAULT_INSTRUMENT_LOCK_TIMEOUT_MS",
    "InstrumentLockPolicy",
    "InstrumentLockRetryExhausted",
    "RETRYABLE_SQLSTATES",
    "execute_instrument_write",
    "instrument_lock_counters",
]
