"""Owner database storage tiers: install | plan | apply | status.

The owner PostgreSQL cluster lives on a 500 GB NVMe budget (the "hot" tier).
Evidence tables that only ever grow -- raw provider observations, intraday
quotes, rule-input snapshots, the edge change journal -- would eat that budget
in months, so every row older than the hot window is moved into a twin table in
the ``stock_cold`` tablespace on the HDD.  Nothing is deleted: the twin holds
the same columns and the ``quant.<table>_all`` view unions both halves for the
rare query that needs the full history.

Four commands, one ASCII JSON receipt on stdout each (the scheduled-task host
console is GBK, so every receipt is escaped to ASCII rather than trusting the
code page):

* ``install``  create the tablespace, the cold twins, the access views, the
  role timeouts and the whole-table cold placements.  Idempotent.
* ``plan``     what ``apply`` would move, per table and per day, plus the space
  policy verdict.  Opens a read-only transaction and writes nothing.
* ``apply``    move the rows, then enforce the space budget, then append one
  JSON object to the run log.  Exit 0 on success, 1 when some table failed
  (a per-table failure never stops the other tables).
* ``status``   usage vs budget, hot/cold row counts and the oldest hot
  timestamp per table.

The planning functions (``hot_cutoff``, ``select_tables``, ``plan_space_moves``,
``parse_bytes``) are pure and unit tested without a database; every database
import is lazy so those tests never need psycopg.

Run with the platform venv:
    G:\\StockPlatform\\current\\.venv\\Scripts\\python.exe scripts/database-storage-tiers.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COLD_TABLESPACE = "stock_cold"
DEFAULT_COLD_DIR = r"G:\StockPlatform\data\pg-cold"
DEFAULT_PGDATA_DIR = r"G:\StockPlatform\data\postgresql16"
DEFAULT_LOG_FILE = r"G:\StockPlatform\logs\storage-tiers.jsonl"
DEFAULT_ENV_FILE = r"G:\StockPlatform\config\runtime.env"
DEFAULT_BUDGET_BYTES = 500 * 1024**3
DEFAULT_BATCH_ROWS = 20_000
DEFAULT_STATEMENT_TIMEOUT_MS = 10 * 60 * 1000
LOCK_TIMEOUT_MS = 30_000

# Space policy thresholds.  Above HIGH_WATER the job gives up the oldest day of
# the largest tiered table until usage is back below TARGET; above ALERT the run
# record is flagged so an operator sees it without reading the whole log.  The
# hot window is never cut below MIN_HOT_DAYS -- at that point the job can only
# alert, because the answer is more disk or a new tier policy, not less history.
HIGH_WATER_RATIO = 0.85
TARGET_RATIO = 0.75
ALERT_RATIO = 0.95
MIN_HOT_DAYS = 30
MAX_SPACE_STEPS = 10_000


@dataclass(frozen=True)
class TierPolicy:
    """One tiered table: rows older than ``hot_days`` belong in the cold twin."""

    schema: str
    table: str
    column: str
    hot_days: int

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def cold_table(self) -> str:
        return f"{self.schema}.{self.table}_cold"

    @property
    def all_view(self) -> str:
        return f"{self.schema}.{self.table}_all"

    def with_hot_days(self, hot_days: int) -> "TierPolicy":
        return TierPolicy(self.schema, self.table, self.column, int(hot_days))


TIER_POLICY: tuple[TierPolicy, ...] = (
    TierPolicy("quant", "raw_market_observations", "available_at", 365),
    TierPolicy("quant", "tushare_raw_records", "available_at", 365),
    TierPolicy("quant", "intraday_quote_observations", "observed_at", 365),
    TierPolicy("quant", "intraday_rule_input_snapshots", "observed_at", 365),
    TierPolicy("quant", "edge_evidence_changes", "changed_at", 365),
)

# Read rarely, never joined on a hot path: the whole table (and its indexes)
# lives in the cold tablespace instead of getting a twin.
WHOLE_TABLE_COLD: tuple[str, ...] = ("quant.legacy_source_records",)

# The peer's read role gets bounded statements and bounded idle transactions so
# a forgotten psql session cannot hold a lock across the maintenance window.
# quant_app / stock_admin are untouched: the owner API sets its own timeouts.
ROLE_SETTINGS: tuple[tuple[str, str, str], ...] = (
    ("stock_peer", "statement_timeout", "15min"),
    ("stock_peer", "idle_in_transaction_session_timeout", "5min"),
)


# --------------------------------------------------------------------------
# Pure planning functions (unit tested without a database)
# --------------------------------------------------------------------------


def hot_cutoff(now: datetime, hot_days: int) -> datetime:
    """Rows strictly older than the returned instant belong in the cold tier.

    ``now`` must be timezone-aware: the tiered columns are ``timestamptz`` and a
    naive cutoff would silently mean "local time" on a machine whose timezone is
    not UTC, moving up to eight hours of rows that are still inside the window.
    """
    if not isinstance(now, datetime):
        raise TypeError("hot_cutoff needs a datetime")
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("hot_cutoff needs a timezone-aware now")
    days = int(hot_days)
    if days < 1:
        raise ValueError("hot_days must be >= 1")
    return now - timedelta(days=days)


def select_tables(policies, names=None) -> tuple[TierPolicy, ...]:
    """Resolve ``--table`` arguments against the policy, in the caller's order.

    A name may be qualified (``quant.raw_market_observations``) or bare
    (``raw_market_observations``).  An unknown name is an error rather than a
    silent no-op: a typo must not make ``apply`` look like it moved nothing.
    """
    policies = tuple(policies)
    if not names:
        return policies
    index: dict[str, TierPolicy] = {}
    for policy in policies:
        index[policy.qualified] = policy
        index[policy.table] = policy
    chosen: list[TierPolicy] = []
    seen: set[str] = set()
    for raw in names:
        key = (raw or "").strip().lower()
        policy = index.get(key)
        if policy is None:
            known = ", ".join(p.qualified for p in policies)
            raise ValueError(f"unknown tiered table {raw!r}; known tables: {known}")
        if policy.qualified not in seen:
            seen.add(policy.qualified)
            chosen.append(policy)
    return tuple(chosen)


def parse_bytes(value, default=None):
    """Accept a plain byte count or a ``<number><unit>`` string (KB/MB/GB/TB)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, (int, float)):
        parsed = int(value)
        if parsed <= 0:
            raise ValueError(f"byte size must be positive: {value!r}")
        return parsed
    text = str(value).strip().upper().replace("_", "")
    multiplier = 1
    for suffix, factor in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024), ("B", 1)):
        if text.endswith(suffix):
            text, multiplier = text[: -len(suffix)].strip(), factor
            break
    try:
        number = float(text)
    except ValueError as error:
        raise ValueError(f"invalid byte size: {value!r}") from error
    parsed = int(number * multiplier)
    if parsed <= 0:
        raise ValueError(f"byte size must be positive: {value!r}")
    return parsed


def plan_space_moves(
    usage,
    budget,
    table_sizes,
    oldest_days,
    *,
    min_hot_days: int = MIN_HOT_DAYS,
    high_water_ratio: float = HIGH_WATER_RATIO,
    target_ratio: float = TARGET_RATIO,
    alert_ratio: float = ALERT_RATIO,
):
    """Pure.  Decide which tiered tables must give up their oldest day(s).

    ``usage``/``budget`` are bytes on the hot data directory, ``table_sizes``
    maps a qualified table name to ``pg_total_relation_size`` and ``oldest_days``
    maps it to the age in days of its oldest hot row (``None`` when the table is
    empty or unreadable).

    Each step hands the largest remaining table's oldest day to the cold tier.
    How many bytes that frees is unknowable without moving it, so the estimate
    assumes the table's bytes are spread evenly over its hot span -- good enough
    to order the steps, and the caller re-measures the directory afterwards.

    Returns a verdict dict; ``table_hot_days`` is what ``apply`` acts on: the
    reduced hot window per table, never below ``min_hot_days``.
    """
    budget_bytes = int(budget or 0)
    if budget_bytes <= 0:
        raise ValueError("budget_bytes must be positive")
    usage_bytes = float(usage)
    if usage_bytes < 0:
        raise ValueError("usage_bytes must not be negative")

    ratio = usage_bytes / budget_bytes
    verdict = {
        "usage_bytes": int(usage_bytes),
        "budget_bytes": budget_bytes,
        "usage_ratio": round(ratio, 6),
        "high_water_ratio": high_water_ratio,
        "target_ratio": target_ratio,
        "alert_ratio": alert_ratio,
        "min_hot_days": int(min_hot_days),
        "status": "ok",
        "alert": ratio > alert_ratio,
        "steps": [],
        "table_hot_days": {},
        "estimated_bytes_freed": 0,
        "estimated_usage_bytes_after": int(usage_bytes),
        "reason": "usage is within the high-water mark",
    }
    if ratio <= high_water_ratio:
        return verdict

    sizes = {name: max(0.0, float(size or 0)) for name, size in (table_sizes or {}).items()}
    days = {}
    for name in sizes:
        age = (oldest_days or {}).get(name)
        days[name] = None if age is None else int(age)

    # A one-byte tolerance: the per-day estimates are floats, so an exact
    # landing on the target must not cost one more day of history to rounding.
    target_bytes = budget_bytes * target_ratio + 1.0
    remaining = usage_bytes
    steps: list[dict] = []
    while remaining > target_bytes and len(steps) < MAX_SPACE_STEPS:
        candidates = [
            name
            for name, size in sizes.items()
            if size > 0 and days.get(name) is not None and days[name] > min_hot_days
        ]
        if not candidates:
            break
        # Largest table first; the name breaks ties so the plan is deterministic.
        name = max(candidates, key=lambda item: (sizes[item], item))
        span = days[name]
        per_day = sizes[name] / span
        sizes[name] = max(0.0, sizes[name] - per_day)
        days[name] = span - 1
        remaining = max(0.0, remaining - per_day)
        steps.append(
            {
                "table": name,
                "hot_days": days[name],
                "estimated_bytes": int(per_day),
                "estimated_usage_bytes_after": int(remaining),
            }
        )

    met_target = remaining <= target_bytes
    verdict["steps"] = steps
    verdict["table_hot_days"] = {step["table"]: step["hot_days"] for step in steps}
    verdict["estimated_bytes_freed"] = int(usage_bytes - remaining)
    verdict["estimated_usage_bytes_after"] = int(remaining)
    if met_target:
        verdict["status"] = "reduce"
        verdict["reason"] = "usage above the high-water mark; shortening the hot window of the largest tables"
    else:
        verdict["status"] = "exhausted"
        verdict["alert"] = True
        verdict["reason"] = (
            f"usage above the high-water mark and no tiered table has history beyond {int(min_hot_days)} "
            "hot days; the hot budget needs more disk or a new tier policy"
        )
    return verdict


# --------------------------------------------------------------------------
# Environment, filesystem and connection helpers
# --------------------------------------------------------------------------


def read_env_file(path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` runtime.env.  Values are never logged or printed."""
    values: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_env(env_file) -> dict[str, str]:
    """Merge runtime.env into the process environment and return the result."""
    if env_file:
        path = Path(env_file)
        if path.is_file():
            loaded = read_env_file(path)
            os.environ.update(loaded)
    return dict(os.environ)


def connect(env, *, autocommit: bool = False, read_only: bool = False, statement_timeout_ms: int | None = None):
    """Open an admin connection.  psycopg is imported lazily (pure tests).

    ``read_only`` is enforced by the server (``default_transaction_read_only``)
    rather than by this script's good intentions, so ``plan``/``status`` cannot
    write to production even through a mistake.
    """
    import psycopg

    quant_service = str(ROOT / "quant-service")
    if quant_service not in sys.path:
        sys.path.insert(0, quant_service)
    from app.db_dsn import connection_params

    params = connection_params(env)
    # The tablespace, the twins and the role settings all need the cluster
    # owner; the application role cannot create a tablespace or ALTER ROLE.
    if env.get("PGADMINUSER"):
        params["user"] = env["PGADMINUSER"]
        params["password"] = env.get("PGADMINPASSWORD", "")
    options = []
    if read_only:
        options.append("-c default_transaction_read_only=on")
    if statement_timeout_ms:
        options.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    if options:
        params["options"] = " ".join(options)
    return psycopg.connect(**params, autocommit=autocommit, application_name="database-storage-tiers")


def directory_size_bytes(path):
    """Recursive on-disk size of the hot data directory, or None if unreadable.

    Files vanish under a live cluster (WAL recycling, temp files), so a failed
    stat skips that entry instead of failing the whole measurement.
    """
    if not path:
        return None
    root = Path(path)
    if not root.is_dir():
        return None
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def resolve_settings(args, env) -> dict:
    """Every tunable: command line beats runtime.env beats the documented default."""
    budget = parse_bytes(args.budget_bytes, None) if getattr(args, "budget_bytes", None) else None
    if budget is None:
        budget = parse_bytes(env.get("PGDATA_BUDGET_BYTES"), DEFAULT_BUDGET_BYTES)
    return {
        "tablespace": getattr(args, "tablespace", None) or COLD_TABLESPACE,
        "pgdata_dir": getattr(args, "pgdata_dir", None) or env.get("PGDATA_DIR") or DEFAULT_PGDATA_DIR,
        "cold_dir": getattr(args, "cold_dir", None)
        or env.get("PGDATA_COLD_TABLESPACE_DIR")
        or DEFAULT_COLD_DIR,
        "budget_bytes": budget,
        "batch_rows": int(getattr(args, "batch", None) or DEFAULT_BATCH_ROWS),
        "statement_timeout_ms": int(getattr(args, "timeout_ms", None) or DEFAULT_STATEMENT_TIMEOUT_MS),
        "max_batches": int(getattr(args, "max_batches", None) or 0),
        "log_file": getattr(args, "log_file", None) or env.get("PGDATA_TIERS_LOG_FILE") or DEFAULT_LOG_FILE,
    }


def _policies(args) -> tuple[TierPolicy, ...]:
    policies = select_tables(TIER_POLICY, getattr(args, "table", None))
    hot_days = getattr(args, "hot_days", None)
    if hot_days:
        policies = tuple(policy.with_hot_days(hot_days) for policy in policies)
    return policies


def _regclass(conn, name):
    return conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0]


def _scalar(conn, query, params=None, default=None):
    """One scalar, or ``default`` when the statement times out or is cancelled.

    ``min(available_at)`` on a 9 GB evidence table has no supporting index; the
    receipt saying "unknown" is better than the whole run failing on it.
    """
    import psycopg

    try:
        with conn.transaction():
            row = conn.execute(query, params or ()).fetchone()
        return default if row is None else row[0]
    except psycopg.errors.QueryCanceled:
        return default
    except psycopg.Error:
        return default


# --------------------------------------------------------------------------
# SQL builders (identifiers come from the frozen policy above)
# --------------------------------------------------------------------------


def _ident(name: str):
    from psycopg import sql

    schema, _, table = name.partition(".")
    return sql.Identifier(schema, table) if table else sql.Identifier(schema)


def move_batch_sql(policy: TierPolicy):
    """One bounded, resumable move: delete a batch and insert it in one statement.

    The ``DELETE ... RETURNING`` feeds the ``INSERT`` inside a single statement,
    so there is no window in which the rows exist in neither table.  The outer
    SELECT counts both sides: a row already present in the twin is deleted from
    hot and skipped by ``ON CONFLICT DO NOTHING``, which is a resumed run
    finishing an interrupted batch, not a loss.
    """
    from psycopg import sql

    return sql.SQL(
        """
        WITH batch AS (
            SELECT ctid FROM {hot} WHERE {column} < %(cutoff)s ORDER BY {column} LIMIT %(batch)s
        ), moved AS (
            DELETE FROM {hot} WHERE ctid IN (SELECT ctid FROM batch) RETURNING *
        ), inserted AS (
            INSERT INTO {cold} SELECT * FROM moved ON CONFLICT DO NOTHING RETURNING 1
        )
        SELECT (SELECT count(*) FROM moved)::bigint, (SELECT count(*) FROM inserted)::bigint
        """
    ).format(hot=_ident(policy.qualified), cold=_ident(policy.cold_table), column=sql.Identifier(policy.column))


def _set_local(conn, name: str, value: str):
    from psycopg import sql

    conn.execute(sql.SQL("SET LOCAL {} = {}").format(sql.Identifier(name), sql.Literal(value)))


# --------------------------------------------------------------------------
# Database reads shared by plan / apply / status
# --------------------------------------------------------------------------


def table_stats(conn, policies, *, now=None, with_counts: bool = False) -> dict:
    """Size, oldest hot row and (optionally) row counts for every tiered table."""
    now = now or datetime.now(timezone.utc)
    stats: dict[str, dict] = {}
    for policy in policies:
        entry = {
            "table": policy.qualified,
            "column": policy.column,
            "hot_days": policy.hot_days,
            "exists": _regclass(conn, policy.qualified) is not None,
            "cold_table": policy.cold_table,
            "cold_exists": _regclass(conn, policy.cold_table) is not None,
            "view_exists": _regclass(conn, policy.all_view) is not None,
            "size_bytes": None,
            "cold_size_bytes": None,
            "oldest_hot_at": None,
            "oldest_hot_days": None,
        }
        if entry["exists"]:
            entry["size_bytes"] = _scalar(conn, "SELECT pg_total_relation_size(%s)", (policy.qualified,))
            oldest = _scalar(
                conn,
                _sql_min_column(policy),
                default=None,
            )
            if oldest is not None:
                entry["oldest_hot_at"] = oldest.isoformat()
                entry["oldest_hot_days"] = max(0, int((now - oldest).total_seconds() // 86400))
            if with_counts:
                entry["hot_rows"], entry["hot_rows_basis"] = _count_rows(conn, policy.qualified)
        if entry["cold_exists"]:
            entry["cold_size_bytes"] = _scalar(conn, "SELECT pg_total_relation_size(%s)", (policy.cold_table,))
            if with_counts:
                entry["cold_rows"], entry["cold_rows_basis"] = _count_rows(conn, policy.cold_table)
        stats[policy.qualified] = entry
    return stats


def _sql_min_column(policy: TierPolicy):
    from psycopg import sql

    return sql.SQL("SELECT min({column}) FROM {hot}").format(
        column=sql.Identifier(policy.column), hot=_ident(policy.qualified)
    )


def _count_rows(conn, qualified: str):
    """Exact count, falling back to the planner estimate when it times out."""
    from psycopg import sql

    exact = _scalar(conn, sql.SQL("SELECT count(*) FROM {}").format(_ident(qualified)))
    if exact is not None:
        return int(exact), "exact"
    estimate = _scalar(conn, "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", (qualified,))
    return (None if estimate is None else int(estimate)), "estimate"


def usage_report(settings) -> dict:
    usage = directory_size_bytes(settings["pgdata_dir"])
    budget = settings["budget_bytes"]
    return {
        "pgdata_dir": str(settings["pgdata_dir"]),
        "usage_bytes": usage,
        "budget_bytes": budget,
        "usage_ratio": None if usage is None else round(usage / budget, 6),
        "measured": usage is not None,
    }


def space_verdict(usage: dict, stats: dict) -> dict:
    """Apply the pure space policy to the measured usage, or say why we cannot."""
    if not usage["measured"]:
        return {
            "status": "unknown",
            "alert": False,
            "steps": [],
            "table_hot_days": {},
            "reason": f"hot data directory not readable: {usage['pgdata_dir']}",
        }
    sizes = {name: entry["size_bytes"] or 0 for name, entry in stats.items() if entry["exists"]}
    oldest = {name: stats[name]["oldest_hot_days"] for name in sizes}
    return plan_space_moves(usage["usage_bytes"], usage["budget_bytes"], sizes, oldest)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def command_install(args, env) -> dict:
    from psycopg import sql

    settings = resolve_settings(args, env)
    policies = _policies(args)
    tablespace = settings["tablespace"]
    report = {
        "command": "install",
        "tablespace": tablespace,
        "tablespace_dir": str(settings["cold_dir"]),
        "actions": [],
        "errors": [],
    }

    def note(action: str, target: str, result: str, **extra):
        report["actions"].append({"action": action, "target": target, "result": result, **extra})

    with connect(env, autocommit=True) as conn:
        # CREATE TABLESPACE cannot run inside a transaction block, hence the
        # autocommit connection for the whole of install.
        exists = conn.execute("SELECT 1 FROM pg_tablespace WHERE spcname = %s", (tablespace,)).fetchone()
        if exists:
            note("create_tablespace", tablespace, "already_exists")
        else:
            cold_dir = Path(settings["cold_dir"])
            if not cold_dir.is_dir():
                raise RuntimeError(f"cold tablespace directory does not exist: {cold_dir}")
            with os.scandir(cold_dir) as entries:
                leftovers = [entry.name for entry in entries if not entry.name.startswith("PG_")]
            if leftovers:
                raise RuntimeError(
                    f"cold tablespace directory is not empty and is not a tablespace: {cold_dir}"
                )
            conn.execute(
                sql.SQL("CREATE TABLESPACE {} LOCATION {}").format(
                    sql.Identifier(tablespace), sql.Literal(str(cold_dir))
                )
            )
            note("create_tablespace", tablespace, "created")
        # The cold tier is spinning rust; tell the planner so it stops choosing
        # index lookups that are only cheap on the NVMe side.
        conn.execute(
            sql.SQL("ALTER TABLESPACE {} SET (random_page_cost = 4, seq_page_cost = 1)").format(
                sql.Identifier(tablespace)
            )
        )
        note("alter_tablespace_options", tablespace, "applied")

        for role, setting, value in ROLE_SETTINGS:
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                note("alter_role", f"{role}.{setting}", "role_missing")
                continue
            conn.execute(
                sql.SQL("ALTER ROLE {} SET {} = {}").format(
                    sql.Identifier(role), sql.Identifier(setting), sql.Literal(value)
                )
            )
            note("alter_role", f"{role}.{setting}", "applied", value=value)

        for policy in policies:
            try:
                _install_twin(conn, policy, tablespace, note)
            except Exception as error:  # noqa: BLE001 - one table must not stop the rest
                report["errors"].append({"target": policy.qualified, "error": _error_text(error)})

        for qualified in WHOLE_TABLE_COLD:
            try:
                _install_whole_table_cold(conn, qualified, tablespace, note)
            except Exception as error:  # noqa: BLE001
                report["errors"].append({"target": qualified, "error": _error_text(error)})

        preload = conn.execute("SHOW shared_preload_libraries").fetchone()[0] or ""
        if "pg_stat_statements" in preload:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
            note("create_extension", "pg_stat_statements", "installed")
        else:
            note(
                "create_extension",
                "pg_stat_statements",
                "skipped_not_preloaded",
                detail="shared_preload_libraries must list pg_stat_statements and the cluster restarted",
            )

    report["status"] = "failed" if report["errors"] else "ok"
    return report


def _install_twin(conn, policy: TierPolicy, tablespace: str, note):
    from psycopg import sql

    if _regclass(conn, policy.qualified) is None:
        note("create_cold_twin", policy.cold_table, "hot_table_missing")
        return
    created = _regclass(conn, policy.cold_table) is None
    if created:
        # default_tablespace makes the copied indexes land in the cold
        # tablespace too; LIKE copies neither foreign keys nor triggers, which
        # is exactly what a twin wants.
        conn.execute(sql.SQL("SET default_tablespace = {}").format(sql.Literal(tablespace)))
        try:
            conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {cold} "
                    "(LIKE {hot} INCLUDING DEFAULTS INCLUDING INDEXES) TABLESPACE {tablespace}"
                ).format(
                    cold=_ident(policy.cold_table),
                    hot=_ident(policy.qualified),
                    tablespace=sql.Identifier(tablespace),
                )
            )
        finally:
            conn.execute("RESET default_tablespace")
    note("create_cold_twin", policy.cold_table, "created" if created else "already_exists")

    moved_indexes = _move_indexes_to_cold(conn, policy.cold_table, tablespace)
    if moved_indexes:
        note("move_indexes", policy.cold_table, "moved", indexes=moved_indexes)

    conn.execute(
        sql.SQL("CREATE OR REPLACE VIEW {view} AS SELECT * FROM {hot} UNION ALL SELECT * FROM {cold}").format(
            view=_ident(policy.all_view), hot=_ident(policy.qualified), cold=_ident(policy.cold_table)
        )
    )
    note("create_view", policy.all_view, "applied")


def _move_indexes_to_cold(conn, qualified: str, tablespace: str) -> list[str]:
    from psycopg import sql

    rows = conn.execute(
        """
        SELECT n.nspname, c.relname
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.indrelid = %s::regclass
          AND c.reltablespace IS DISTINCT FROM (SELECT oid FROM pg_tablespace WHERE spcname = %s)
        ORDER BY 1, 2
        """,
        (qualified, tablespace),
    ).fetchall()
    moved = []
    for schema, index_name in rows:
        conn.execute(
            sql.SQL("ALTER INDEX {} SET TABLESPACE {}").format(
                sql.Identifier(schema, index_name), sql.Identifier(tablespace)
            )
        )
        moved.append(f"{schema}.{index_name}")
    return moved


def _install_whole_table_cold(conn, qualified: str, tablespace: str, note):
    from psycopg import sql

    if _regclass(conn, qualified) is None:
        note("move_table_to_cold", qualified, "table_missing")
        return
    current = conn.execute(
        """
        SELECT coalesce(t.spcname, '')
        FROM pg_class c LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
        WHERE c.oid = %s::regclass
        """,
        (qualified,),
    ).fetchone()[0]
    if current == tablespace:
        note("move_table_to_cold", qualified, "already_cold")
    else:
        conn.execute(
            sql.SQL("ALTER TABLE {} SET TABLESPACE {}").format(_ident(qualified), sql.Identifier(tablespace))
        )
        note("move_table_to_cold", qualified, "moved")
    moved_indexes = _move_indexes_to_cold(conn, qualified, tablespace)
    if moved_indexes:
        note("move_indexes", qualified, "moved", indexes=moved_indexes)


def command_plan(args, env) -> dict:
    from psycopg import sql

    settings = resolve_settings(args, env)
    policies = _policies(args)
    now = datetime.now(timezone.utc)
    usage = usage_report(settings)
    report = {
        "command": "plan",
        "generated_at": now.isoformat(),
        "batch_rows": settings["batch_rows"],
        "usage": usage,
        "tables": [],
    }
    with connect(
        env, autocommit=True, read_only=True, statement_timeout_ms=settings["statement_timeout_ms"]
    ) as conn:
        stats = table_stats(conn, policies, now=now)
        for policy in policies:
            entry = dict(stats[policy.qualified])
            cutoff = hot_cutoff(now, policy.hot_days)
            entry["cutoff"] = cutoff.isoformat()
            entry["rows_to_move"] = None
            entry["days"] = []
            if entry["exists"]:
                entry["rows_to_move"] = _scalar(
                    conn,
                    sql.SQL("SELECT count(*) FROM {hot} WHERE {column} < %s").format(
                        hot=_ident(policy.qualified), column=sql.Identifier(policy.column)
                    ),
                    (cutoff,),
                )
                entry["days"] = _rows_per_day(conn, policy, cutoff, int(getattr(args, "day_limit", 0) or 30))
            report["tables"].append(entry)
        report["space_policy"] = space_verdict(usage, stats)
    report["status"] = "ok"
    return report


def _rows_per_day(conn, policy: TierPolicy, cutoff, limit: int):
    from psycopg import sql

    query = sql.SQL(
        "SELECT date_trunc('day', {column} AT TIME ZONE 'UTC')::date AS day, count(*)::bigint "
        "FROM {hot} WHERE {column} < %s GROUP BY 1 ORDER BY 1 LIMIT %s"
    ).format(column=sql.Identifier(policy.column), hot=_ident(policy.qualified))
    try:
        with conn.transaction():
            rows = conn.execute(query, (cutoff, max(1, limit))).fetchall()
    except Exception:  # noqa: BLE001 - a per-day breakdown is a nicety, not the plan
        return []
    return [{"day": str(day), "rows": int(count)} for day, count in rows]


def command_apply(args, env) -> dict:
    settings = resolve_settings(args, env)
    policies = _policies(args)
    started_at = datetime.now(timezone.utc)
    usage_before = usage_report(settings)
    record = {
        "command": "apply",
        "started_at": started_at.isoformat(),
        "batch_rows": settings["batch_rows"],
        "usage_before": usage_before,
        "tables": [],
        "errors": [],
    }
    with connect(env, autocommit=True, statement_timeout_ms=settings["statement_timeout_ms"]) as conn:
        stats_before = table_stats(conn, policies, now=started_at)
        record["space_policy_before"] = space_verdict(usage_before, stats_before)

        results: dict[str, dict] = {}
        for policy in policies:
            results[policy.qualified] = _move_table(conn, policy, started_at, policy.hot_days, settings, record)

        usage_mid = usage_report(settings)
        stats_mid = table_stats(conn, policies, now=started_at)
        verdict = space_verdict(usage_mid, stats_mid)
        record["usage_after_time_moves"] = usage_mid
        record["space_policy"] = verdict
        by_name = {policy.qualified: policy for policy in policies}
        for name, hot_days in sorted(verdict.get("table_hot_days", {}).items()):
            policy = by_name.get(name)
            if policy is None:
                continue
            extra = _move_table(conn, policy, started_at, hot_days, settings, record, reason="space_policy")
            merged = results.get(name)
            if merged is None:
                results[name] = extra
            else:
                merged["deleted_rows"] += extra["deleted_rows"]
                merged["inserted_rows"] += extra["inserted_rows"]
                merged["batches"] += extra["batches"]
                merged["space_policy_hot_days"] = hot_days

        record["tables"] = [results[policy.qualified] for policy in policies if policy.qualified in results]
        record["usage_after"] = usage_report(settings)

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["moved_rows"] = sum(entry["deleted_rows"] for entry in record["tables"])
    record["status"] = "partial" if record["errors"] else "ok"
    record["alert"] = bool(record["space_policy"].get("alert") or record["space_policy_before"].get("alert"))
    _append_jsonl(settings["log_file"], record)
    return record


def _move_table(conn, policy: TierPolicy, now, hot_days: int, settings, record, reason: str = "hot_window") -> dict:
    """Move everything older than the cutoff, one bounded batch per transaction."""
    from psycopg import sql

    cutoff = hot_cutoff(now, hot_days)
    result = {
        "table": policy.qualified,
        "column": policy.column,
        "hot_days": int(hot_days),
        "cutoff": cutoff.isoformat(),
        "reason": reason,
        "deleted_rows": 0,
        "inserted_rows": 0,
        "batches": 0,
        "status": "ok",
    }
    try:
        if _regclass(conn, policy.qualified) is None or _regclass(conn, policy.cold_table) is None:
            result["status"] = "skipped_missing_table"
            return result
        statement = move_batch_sql(policy)
        max_batches = int(settings.get("max_batches") or 0)
        while True:
            with conn.transaction():
                _set_local(conn, "statement_timeout", str(settings["statement_timeout_ms"]))
                _set_local(conn, "lock_timeout", str(LOCK_TIMEOUT_MS))
                deleted, inserted = conn.execute(
                    statement, {"cutoff": cutoff, "batch": settings["batch_rows"]}
                ).fetchone()
            result["deleted_rows"] += int(deleted)
            result["inserted_rows"] += int(inserted)
            result["batches"] += 1
            if int(deleted) == 0:
                break
            if max_batches and result["batches"] >= max_batches:
                result["status"] = "batch_limit_reached"
                break
        if result["deleted_rows"] > result["inserted_rows"]:
            # Rows the twin already held: an earlier interrupted run finished.
            result["already_in_cold_rows"] = result["deleted_rows"] - result["inserted_rows"]
        if result["deleted_rows"] > 0:
            conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(_ident(policy.qualified)))
            result["vacuumed"] = True
    except Exception as error:  # noqa: BLE001 - per-table isolation is the contract
        result["status"] = "failed"
        result["error"] = _error_text(error)
        record["errors"].append({"table": policy.qualified, "error": result["error"]})
    return result


def command_status(args, env) -> dict:
    settings = resolve_settings(args, env)
    policies = _policies(args)
    now = datetime.now(timezone.utc)
    usage = usage_report(settings)
    report = {
        "command": "status",
        "generated_at": now.isoformat(),
        "tablespace": settings["tablespace"],
        "usage": usage,
        "tables": [],
    }
    with connect(
        env, autocommit=True, read_only=True, statement_timeout_ms=settings["statement_timeout_ms"]
    ) as conn:
        row = conn.execute(
            "SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace WHERE spcname = %s",
            (settings["tablespace"],),
        ).fetchone()
        report["tablespace_installed"] = row is not None
        report["tablespace_location"] = None if row is None else row[1]
        stats = table_stats(conn, policies, now=now, with_counts=True)
        report["tables"] = [stats[policy.qualified] for policy in policies]
        report["whole_table_cold"] = [
            {
                "table": qualified,
                "exists": _regclass(conn, qualified) is not None,
                "tablespace": _scalar(
                    conn,
                    "SELECT coalesce(t.spcname, 'pg_default') FROM pg_class c "
                    "LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace WHERE c.oid = %s::regclass",
                    (qualified,),
                ),
            }
            for qualified in WHOLE_TABLE_COLD
        ]
        report["space_policy"] = space_verdict(usage, stats)
    report["status"] = "ok"
    return report


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error).strip()[:300]}"


def _append_jsonl(path, record: dict):
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
    except OSError as error:
        record["log_write_error"] = _error_text(error)


def _ascii_streams(*streams):
    """The scheduled-task host console is GBK; every receipt stays ASCII."""
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="ascii", errors="backslashreplace")
            except (ValueError, OSError):
                pass


COMMANDS = {
    "install": command_install,
    "plan": command_plan,
    "apply": command_apply,
    "status": command_status,
}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="runtime.env to load into the process env")
    common.add_argument("--hot-days", type=int, help="override the hot window of every selected table")
    common.add_argument("--budget-bytes", help="hot tier budget; accepts 500GB or a byte count")
    common.add_argument("--batch", type=int, help=f"rows per move transaction (default {DEFAULT_BATCH_ROWS})")
    common.add_argument("--table", action="append", help="repeatable; limit to one tiered table")
    common.add_argument("--pgdata-dir", help="hot data directory to measure (default PGDATA_DIR)")
    common.add_argument("--cold-dir", help="cold tablespace directory (default PGDATA_COLD_TABLESPACE_DIR)")
    common.add_argument(
        "--tablespace",
        default=COLD_TABLESPACE,
        help=f"cold tablespace name (default {COLD_TABLESPACE}); a test uses its own so it never "
        "squats on the production tablespace",
    )
    common.add_argument("--timeout-ms", type=int, help="statement_timeout for reads and move batches")
    common.add_argument("--max-batches", type=int, help="stop a table after this many move batches (0 = unlimited)")
    common.add_argument("--log-file", help=f"apply run log (default {DEFAULT_LOG_FILE})")

    parser = argparse.ArgumentParser(
        prog="database-storage-tiers",
        description="Owner PostgreSQL hot/cold storage tiers: install, plan, apply, status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", parents=[common], help="create the cold tablespace, twins, views and role timeouts")
    plan = sub.add_parser("plan", parents=[common], help="what apply would move; writes nothing")
    plan.add_argument("--day-limit", type=int, default=30, help="how many oldest days to break down per table")
    sub.add_parser("apply", parents=[common], help="move the rows and enforce the space budget")
    sub.add_parser("status", parents=[common], help="usage vs budget and hot/cold counts")
    return parser


def main(argv=None) -> int:
    _ascii_streams(sys.stdout, sys.stderr)
    args = build_parser().parse_args(argv)
    env = load_env(args.env_file)
    try:
        report = COMMANDS[args.command](args, env)
    except Exception as error:  # noqa: BLE001 - the receipt is the interface, including on failure
        print(json.dumps({"command": args.command, "status": "failed", "error": _error_text(error)},
                         ensure_ascii=True, default=str))
        return 2
    print(json.dumps(report, ensure_ascii=True, default=str))
    return 1 if report.get("status") not in ("ok",) else 0


if __name__ == "__main__":
    raise SystemExit(main())
