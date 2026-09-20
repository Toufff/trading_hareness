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
  cutoff indexes, the quarantine table, the role timeouts and the whole-table
  cold placements.  Idempotent, and it reconciles a twin whose hot table gained
  columns since the twin was created.
* ``plan``     what ``apply`` would move, per table and per day, plus the space
  policy verdict.  Opens a read-only transaction and writes nothing.
* ``apply``    move the rows, then enforce the space budget, then append one
  JSON object to the run log.
* ``status``   usage vs budget, hot/cold row counts and the oldest hot
  timestamp per table.

Exit codes: 0 ok (including ``deadline_reached``), 1 something needs a human
but the tier is intact (``partial``, ``conflicts``, ``schema_drift``,
``deadline_missed``), 2 the budget guard is not doing its job (``degraded``:
usage unmeasurable, the hot window exhausted, the ratchet stalled ->
``needs_repack``, or -- for ``plan``/``status`` -- ``install`` has not run).

Four properties this job must keep, and how:

1. **No row is ever in neither table.**  A batch is snapshotted into a TEMP
   table, inserted into the twin with an explicit column list, and only then
   deleted from the hot table by primary key -- all inside one transaction.
   A crash anywhere rolls the whole batch back; the run resumes from the same
   cutoff next time.
2. **A natural-key collision never silently destroys a row.**  A hot row whose
   unique key already exists in the twin with *different* content is copied
   into ``quant.storage_tier_conflicts`` (hot primary key, whole hot row, whole
   cold row) before it leaves the hot table, and the run reports
   ``status='conflicts'`` with ``alert``.  Quarantined rows are removed from hot
   so they are not retried forever; ``quant.storage_tier_conflicts`` lives in
   the *default* tablespace on purpose, so the nightly ``pg_dump`` captures it
   (it is not a ``_cold`` twin and is never in the dump exclusion list).
3. **The space ratchet is bounded and honest.**  Plain ``VACUUM`` (never FULL)
   does not return pages to the filesystem: deleting the oldest rows frees pages
   at the *front* of the heap, which PostgreSQL happily reuses for new inserts
   but cannot truncate.  So a rolling window bounds growth -- the files stop
   growing -- while the measured directory size does not fall.  The job
   therefore takes at most ``--max-space-days`` days per table per run,
   re-measures after every table, and stops the ratchet with
   ``status='needs_repack'`` (never running VACUUM FULL or pg_repack itself)
   when the measured usage does not fall by at least 1 %.  Reclaiming the
   existing bloat is an operator decision with its own maintenance window.
   The 1 % is measured on ``tiering_usage_bytes`` (the WAL-free quantity the
   policy itself is expressed in), because the move's own WAL would otherwise
   make the drop negative and stop the ratchet for the wrong reason.
4. **A moved row is always in some backup.**  The nightly ``pg_dump`` excludes
   the data of every table in ``STOCK_BACKUP_INCREMENTAL_TABLES`` and of its
   ``_cold`` twin, because the chunk chain under
   ``<backup root>\\incremental\\<table>\\`` holds their history instead.  That
   is only true up to the chain's watermark, so for such a table the move is
   clamped: no row whose ``created_at`` (or ``updated_at``) has reached the
   watermark is moved, the run reports ``chain_behind`` when the clamp actually
   withheld rows, and a table whose ``state.json`` cannot be read moves nothing
   at all (``chain_missing``, with an alert).

The planning functions (``hot_cutoff``, ``select_tables``, ``plan_space_moves``,
``parse_bytes``, ``resolve_deadline``, ``effective_budget``, ``schema_drift``,
``parse_incremental_specs``, ``parse_chain_watermark``) are pure and unit tested
without a database; every database import is lazy so those tests never need
psycopg.

Run with the platform venv:
    G:\\StockPlatform\\current\\.venv\\Scripts\\python.exe scripts/database-storage-tiers.py status
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat as stat_module
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
DEFAULT_BACKUP_ROOT = r"G:\StockPlatform\backups"
# scripts/windows/stock-incremental-backup.psm1 exports these tables by window
# into backups\incremental\<table>\, so the nightly dump excludes their data --
# and, once a twin exists, the twin's data too.  Same spelling and same default
# as Get-StockIncrementalTableSpecs, because the two must agree about which
# tables are covered by a chunk chain.
DEFAULT_INCREMENTAL_TABLES = "quant.raw_market_observations:created_at:updated_at"
DEFAULT_BUDGET_BYTES = 500 * 1024**3
DEFAULT_BATCH_ROWS = 20_000
DEFAULT_STATEMENT_TIMEOUT_MS = 10 * 60 * 1000
LOCK_TIMEOUT_MS = 30_000
# ALTER TABLE ... SET TABLESPACE on quant.legacy_source_records rewrites 2.6 GB
# under AccessExclusiveLock; CREATE INDEX CONCURRENTLY on the 19 GB
# raw_market_observations is two full passes.  Both need a ceiling that is
# generous enough to finish inside the maintenance window and small enough that
# a wedged statement cannot run into the trading session.
INSTALL_STATEMENT_TIMEOUT_MS = 60 * 60 * 1000
INDEX_STATEMENT_TIMEOUT_MS = 2 * 60 * 60 * 1000

# The quarantine table: a hot row whose natural key collides with a *different*
# row already in the twin.  Default tablespace on purpose -- see the module
# docstring.
QUARANTINE_TABLE = "quant.storage_tier_conflicts"

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
# At most a week of history leaves a single table in one run: the estimate of
# "bytes per day" is a guess, and a run that guesses badly must not be able to
# hand a year of history to the cold tier before anybody reads the receipt.
DEFAULT_MAX_SPACE_DAYS = 7
# Plain VACUUM cannot shrink the files, so "the measurement did not move" is the
# normal outcome and the ratchet must stop instead of cutting more history.
MIN_USAGE_DROP_RATIO = 0.01

# The "how many rows is the chain holding back" probe stops counting here: the
# receipt only needs to say "some, and roughly how many", and an unbounded count
# over a table whose chain froze months ago is a full scan.
CHAIN_PROBE_ROWS = 100_000

# VACUUM is not part of the move's correctness -- the rows are already in the
# twin when it runs -- so it gets its own ceiling instead of the batch timeout.
VACUUM_STATEMENT_TIMEOUT_MS = 30 * 60 * 1000

# Space verdicts that mean the 500 GB guard is not actually guarding anything.
# They make the whole run 'degraded' (exit 2) so the scheduled task reports it.
DEGRADED_SPACE_STATUSES = frozenset({"unknown", "exhausted", "needs_repack"})

# Per-table outcomes that mean `install` has not run (or has not finished), so
# the budget guard moved nothing at all.  A run that reports these must never
# exit 0: today's production state is exactly this, and a green scheduled task
# would hide it forever.
NOT_INSTALLED_TABLE_STATUSES = frozenset({"skipped_missing_table", "skipped_missing_quarantine"})

# Per-table outcomes that need a human before the next run: either the backup
# chain that the twin's dump exclusion depends on is not where it should be, or
# the twin carries a unique index this job cannot reason about.
BLOCKING_TABLE_STATUSES = frozenset(
    {"chain_missing", "chain_columns_missing", "unsupported_unique_index"}
)

# Receipt status -> process exit code.
EXIT_CODES = {
    "ok": 0,
    "deadline_reached": 0,
    "deadline_missed": 1,
    "partial": 1,
    "conflicts": 1,
    "schema_drift": 1,
    "degraded": 2,
    "failed": 2,
}


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

    @property
    def cutoff_index(self) -> str:
        """Index that makes ``WHERE <column> < cutoff ORDER BY <column>`` a range scan.

        Without it every 20 000-row batch re-scans and re-sorts a whole index;
        ``install`` and migration 20260919_0106 create exactly this name.
        """
        return f"{self.table}_tier_cutoff_idx"

    def with_hot_days(self, hot_days: int) -> "TierPolicy":
        return TierPolicy(self.schema, self.table, self.column, int(hot_days))


TIER_POLICY: tuple[TierPolicy, ...] = (
    TierPolicy("quant", "raw_market_observations", "available_at", 365),
    TierPolicy("quant", "tushare_raw_records", "available_at", 365),
    TierPolicy("quant", "intraday_quote_observations", "observed_at", 365),
    TierPolicy("quant", "intraday_rule_input_snapshots", "observed_at", 365),
    TierPolicy("quant", "edge_evidence_changes", "changed_at", 365),
    TierPolicy("quant", "trade_thesis_evaluations", "created_at", 365),
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


def parse_clock(value: str) -> tuple[int, int]:
    """``HH:MM`` -> ``(hour, minute)``; anything else is an error."""
    text = str(value or "").strip()
    hour_text, _, minute_text = text.partition(":")
    try:
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as error:
        raise ValueError(f"invalid clock time {value!r}; expected HH:MM") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid clock time {value!r}; expected HH:MM")
    return hour, minute


def resolve_deadline(now: datetime, clock=None, max_seconds=None):
    """The instant at which ``apply`` must stop, or ``None`` for no deadline.

    ``clock`` is a wall-clock ``HH:MM`` read in ``now``'s own timezone -- the
    maintenance window is a local-time concept ("stop at 08:00", before the
    pre-open jobs want the disk).  A clock time that has already passed today is
    taken literally (the deadline is behind us, so the run stops immediately and
    writes its receipt) *unless* it is more than twelve hours behind, which is
    the "started at 23:00, stop at 08:00" case and means tomorrow.

    ``max_seconds`` is a relative cap; when both are given the earlier wins.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("resolve_deadline needs a timezone-aware now")
    candidates = []
    if clock:
        hour, minute = parse_clock(clock)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now and (now - candidate) >= timedelta(hours=12):
            candidate = candidate + timedelta(days=1)
        candidates.append(candidate)
    if max_seconds:
        seconds = int(max_seconds)
        if seconds <= 0:
            raise ValueError("max_seconds must be positive")
        candidates.append(now + timedelta(seconds=seconds))
    return min(candidates) if candidates else None


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


def effective_budget(configured, usage, volume_free):
    """Pure.  The budget the space policy may actually spend.

    ``PGDATA_BUDGET_BYTES`` is an assumption about a drive nobody asked.  The
    real ceiling is what the volume can still deliver, ``usage + free``: a
    500 GB budget on a 480 GB volume would report "within the high-water mark"
    right up to the PANIC.  Returns the effective budget and whether the
    configured one fits; ``fits`` is ``None`` when the volume is unknown.
    """
    configured_bytes = int(configured or 0)
    if configured_bytes <= 0:
        raise ValueError("configured budget must be positive")
    if usage is None or volume_free is None:
        return {"effective_bytes": configured_bytes, "fits_volume": None, "capacity_bytes": None}
    capacity = max(0, int(usage)) + max(0, int(volume_free))
    return {
        "effective_bytes": min(configured_bytes, capacity),
        "fits_volume": capacity >= configured_bytes,
        "capacity_bytes": capacity,
    }


def plan_space_moves(
    usage,
    budget,
    table_sizes,
    oldest_days,
    *,
    min_hot_days: int = MIN_HOT_DAYS,
    max_days_per_table: int = DEFAULT_MAX_SPACE_DAYS,
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

    ``max_days_per_table`` bounds one run: a table gives up at most that many
    days however wrong the estimate is, and the remainder waits for tomorrow.
    When the cap (rather than the ``min_hot_days`` floor) is what stopped the
    plan the status is ``capped``, which is a working state, not a failure.

    Returns a verdict dict; ``table_hot_days`` is what ``apply`` acts on: the
    reduced hot window per table, never below ``min_hot_days``.
    """
    budget_bytes = int(budget or 0)
    if budget_bytes <= 0:
        raise ValueError("budget_bytes must be positive")
    usage_bytes = float(usage)
    if usage_bytes < 0:
        raise ValueError("usage_bytes must not be negative")
    day_cap = int(max_days_per_table)
    if day_cap < 1:
        raise ValueError("max_days_per_table must be >= 1")

    ratio = usage_bytes / budget_bytes
    verdict = {
        "usage_bytes": int(usage_bytes),
        "budget_bytes": budget_bytes,
        "usage_ratio": round(ratio, 6),
        "high_water_ratio": high_water_ratio,
        "target_ratio": target_ratio,
        "alert_ratio": alert_ratio,
        "min_hot_days": int(min_hot_days),
        "max_days_per_table": day_cap,
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
    spent: dict[str, int] = {name: 0 for name in sizes}

    # A one-byte tolerance: the per-day estimates are floats, so an exact
    # landing on the target must not cost one more day of history to rounding.
    target_bytes = budget_bytes * target_ratio + 1.0
    remaining = usage_bytes
    steps: list[dict] = []
    capped = False
    while remaining > target_bytes and len(steps) < MAX_SPACE_STEPS:
        candidates = []
        for name, size in sizes.items():
            if size <= 0 or days.get(name) is None or days[name] <= min_hot_days:
                continue
            if spent[name] >= day_cap:
                capped = True
                continue
            candidates.append(name)
        if not candidates:
            break
        # Largest table first; the name breaks ties so the plan is deterministic.
        name = max(candidates, key=lambda item: (sizes[item], item))
        span = days[name]
        per_day = sizes[name] / span
        sizes[name] = max(0.0, sizes[name] - per_day)
        days[name] = span - 1
        spent[name] += 1
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
    elif capped:
        verdict["status"] = "capped"
        verdict["reason"] = (
            f"usage above the high-water mark; every candidate table already gives up its {day_cap}-day "
            "per-run maximum, the rest waits for the next run"
        )
    else:
        verdict["status"] = "exhausted"
        verdict["alert"] = True
        verdict["reason"] = (
            f"usage above the high-water mark and no tiered table has history beyond {int(min_hot_days)} "
            "hot days; the hot budget needs more disk or a new tier policy"
        )
    return verdict


def schema_drift(hot_columns, cold_columns):
    """Pure.  Compare two ``[(name, type), ...]`` column lists.

    The twin is filled with an explicit column list and read through a view with
    an explicit column list, so a hot table that gained a column is repairable
    (``ADD COLUMN`` on the twin, recreate the view) while a twin that has a
    column the hot table lost, or a type that changed underneath, is not: that
    one has to stop the move rather than write into the wrong column.
    """
    hot = list(hot_columns or [])
    cold = list(cold_columns or [])
    hot_types = dict(hot)
    cold_types = dict(cold)
    missing_in_cold = [name for name, _ in hot if name not in cold_types]
    extra_in_cold = [name for name, _ in cold if name not in hot_types]
    type_mismatch = [
        {"column": name, "hot": hot_types[name], "cold": cold_types[name]}
        for name, _ in hot
        if name in cold_types and hot_types[name] != cold_types[name]
    ]
    return {
        "missing_in_cold": missing_in_cold,
        "extra_in_cold": extra_in_cold,
        "type_mismatch": type_mismatch,
        # Repairable by install: the twin only lacks columns the hot table has.
        "repairable": bool(missing_in_cold) and not extra_in_cold and not type_mismatch,
        "blocking": bool(extra_in_cold or type_mismatch or missing_in_cold),
    }


def parse_incremental_specs(value):
    """Pure.  ``STOCK_BACKUP_INCREMENTAL_TABLES`` -> ``{table: (created, updated|None)}``.

    The same grammar ``Get-StockIncrementalTableSpecs`` parses in
    ``scripts/windows/stock-incremental-backup.psm1``:
    ``schema.table:created_column[:updated_column]`` entries separated by
    ``;``, an empty value meaning the documented default and the literal
    ``none`` meaning "no table is exported incrementally".

    Why this script cares: the nightly ``pg_dump`` excludes the *data* of these
    tables and of their ``_cold`` twins, because the chunk chain is what holds
    their history.  A row this job moves into a twin before the chain has
    exported it would therefore be in no backup at all, so the move is clamped
    to the chain's watermark (see :func:`chain_clamp`).

    A malformed entry raises: guessing what an operator meant would silently
    turn the clamp off for the one table it exists to protect.
    """
    text = "" if value is None else str(value).strip()
    if not text:
        text = DEFAULT_INCREMENTAL_TABLES
    if text.lower() == "none":
        return {}
    specs: dict[str, tuple[str, str | None]] = {}
    for entry in text.split(";"):
        item = entry.strip()
        if not item:
            continue
        parts = [part.strip() for part in item.split(":")]
        if len(parts) not in (2, 3) or parts[0].count(".") != 1 or not all(parts):
            raise ValueError(
                f"invalid incremental table spec {item!r}; expected schema.table:created_column"
                "[:updated_column]"
            )
        table = parts[0].lower()
        if table in specs:
            raise ValueError(f"duplicate incremental table spec for {table}")
        specs[table] = (parts[1], parts[2] if len(parts) == 3 else None)
    return specs


def parse_chain_watermark(payload):
    """Pure.  The ``watermark`` of a chunk chain's ``state.json``, or ``None``.

    ``Invoke-StockIncrementalBackup`` writes
    ``{"table": ..., "watermark": "YYYY-MM-DDTHH:MM:SS.ffffffZ", "updated_at": ...}``
    and advances the watermark only after a chunk's row count was verified, so
    everything strictly below it is exported.  The value is always UTC; a
    naive or unparsable one is refused rather than guessed, because a wrong
    clamp either moves unbacked rows or freezes the tier job.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("watermark")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def chain_state_path(backup_root, qualified: str) -> Path:
    """``<backup root>\\incremental\\<schema.table>\\state.json`` -- the psm1 layout."""
    return Path(backup_root or DEFAULT_BACKUP_ROOT) / "incremental" / qualified / "state.json"


def read_chain_state(backup_root, qualified: str) -> dict:
    """The chunk chain's watermark for one table, and why it is missing if it is.

    A missing directory, a missing file, unreadable JSON or a state without a
    watermark all mean the same thing operationally: this run cannot prove the
    chain has exported anything, so it must not move rows whose only remaining
    copy would be the dump-excluded twin.
    """
    path = chain_state_path(backup_root, qualified)
    entry = {"table": qualified, "state_file": str(path), "watermark": None, "reason": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        entry["reason"] = "no chunk-chain state file; the incremental export has never completed"
        return entry
    except (OSError, ValueError) as error:
        entry["reason"] = f"chunk-chain state file unreadable: {_error_text(error)}"
        return entry
    try:
        watermark = parse_chain_watermark(payload)
    except ValueError as error:
        entry["reason"] = f"chunk-chain watermark unparsable: {_error_text(error)}"
        return entry
    if watermark is None:
        entry["reason"] = "chunk-chain state file carries no watermark"
        return entry
    entry["watermark"] = watermark
    return entry


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
    write to production even through a mistake.  Every connection carries a
    ``lock_timeout``: this job takes AccessExclusiveLock on multi-gigabyte
    tables and must queue behind nobody.
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
    options = [f"-c lock_timeout={LOCK_TIMEOUT_MS}"]
    if read_only:
        options.append("-c default_transaction_read_only=on")
    if statement_timeout_ms:
        options.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    params["options"] = " ".join(options)
    return psycopg.connect(**params, autocommit=autocommit, application_name="database-storage-tiers")


_REPARSE_POINT = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_reparse_point(entry) -> bool:
    """True for a junction, a symlink or anything whose type cannot be read.

    PostgreSQL on Windows puts ``PGDATA\\pg_tblspc\\<oid>`` as a *directory
    junction* pointing at the tablespace -- for this cluster, the whole cold
    tier on the G: HDD.  ``os.scandir`` walks straight through a junction, so a
    naive recursive size would count the cold tier as hot usage and the 500 GB
    cap would be measured against the wrong number.  An entry whose attributes
    cannot be read is treated as a reparse point too: skipping bytes is a
    smaller error than counting a whole other volume.
    """
    is_junction = getattr(entry, "is_junction", None)
    try:
        if is_junction is not None and is_junction():
            return True
        if entry.is_symlink():
            return True
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & _REPARSE_POINT)


def directory_size_bytes(path, *, collect_reparse_points=None):
    """Recursive on-disk size of a directory, or None when it is unreadable.

    Reparse points (junctions, symlinks) are never descended and never counted:
    see :func:`is_reparse_point`.  Files vanish under a live cluster (WAL
    recycling, temp files), so a failed stat skips that entry instead of failing
    the whole measurement.  ``collect_reparse_points`` receives the relative path
    of every skipped reparse point so the receipt can name them.
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
                        if is_reparse_point(entry):
                            if collect_reparse_points is not None:
                                collect_reparse_points.append(os.path.relpath(entry.path, str(root)))
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def measure_pgdata(pgdata_dir) -> dict:
    """Hot usage, WAL usage and the volume behind the hot data directory.

    ``pg_wal`` is measured separately and excluded from the tiering decision:
    its size is bounded by ``max_wal_size``/``wal_keep_size``, a backfill can
    spike it by tens of gigabytes for an hour, and moving evidence rows into the
    cold tier cannot shrink it.  It is still part of ``usage_bytes`` because it
    is genuinely occupying the hot volume.
    """
    root = Path(pgdata_dir) if pgdata_dir else None
    reparse: list[str] = []
    total = directory_size_bytes(root, collect_reparse_points=reparse)
    wal = None if total is None else directory_size_bytes(Path(root) / "pg_wal")
    volume_total = volume_free = None
    if root is not None:
        try:
            usage = shutil.disk_usage(str(root))
            volume_total, volume_free = int(usage.total), int(usage.free)
        except OSError:
            volume_total = volume_free = None
    return {
        "measured": total is not None,
        "usage_bytes": total,
        "wal_bytes": wal,
        "tiering_usage_bytes": None if total is None else max(0, total - (wal or 0)),
        "excluded_reparse_points": sorted(reparse),
        "volume_total_bytes": volume_total,
        "volume_free_bytes": volume_free,
    }


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
        "max_space_days": int(getattr(args, "max_space_days", None) or DEFAULT_MAX_SPACE_DAYS),
        "log_file": getattr(args, "log_file", None) or env.get("PGDATA_TIERS_LOG_FILE") or DEFAULT_LOG_FILE,
        "backup_root": getattr(args, "backup_root", None) or env.get("STOCK_BACKUP_ROOT") or DEFAULT_BACKUP_ROOT,
        # Which tables the nightly dump exports through a chunk chain instead of
        # its own data; their twins' moves are clamped to the chain watermark.
        "incremental_specs": parse_incremental_specs(env.get("STOCK_BACKUP_INCREMENTAL_TABLES")),
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
# Catalog reads: columns, keys, indexes
# --------------------------------------------------------------------------


def table_columns(conn, qualified: str) -> list[tuple[str, str]]:
    """``[(name, formatted type), ...]`` in attribute order, dropped ones excluded."""
    rows = conn.execute(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a
        WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (qualified,),
    ).fetchall()
    return [(name, kind) for name, kind in rows]


def primary_key_columns(conn, qualified: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey::smallint[])
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY array_position(i.indkey::smallint[], a.attnum)
        """,
        (qualified,),
    ).fetchall()
    return [name for (name,) in rows]


def unique_key_columns(conn, qualified: str) -> list[tuple[str, ...]]:
    """Every unique (non-partial, non-expression) key, primary key first.

    These are the keys a move can collide on, because the twin inherits the hot
    table's unique constraints through ``LIKE ... INCLUDING INDEXES``.
    """
    rows = conn.execute(
        """
        SELECT i.indexrelid::regclass::text, i.indisprimary,
               array_agg(a.attname ORDER BY array_position(i.indkey::smallint[], a.attnum)) AS columns,
               i.indnkeyatts
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey::smallint[])
        WHERE i.indrelid = %s::regclass AND i.indisunique AND i.indisvalid
          AND i.indpred IS NULL AND i.indexprs IS NULL
        GROUP BY i.indexrelid, i.indisprimary, i.indnkeyatts
        ORDER BY i.indisprimary DESC, 1
        """,
        (qualified,),
    ).fetchall()
    keys: list[tuple[str, ...]] = []
    for _name, _primary, columns, nkeyatts in rows:
        if len(columns) != int(nkeyatts):
            continue  # an INCLUDE column would make the join wrong
        key = tuple(columns)
        if key not in keys:
            keys.append(key)
    return keys


def unsupported_unique_indexes(conn, qualified: str) -> list[str]:
    """Valid unique indexes this job cannot reason about: partial or expression.

    :func:`unique_key_columns` deliberately skips them -- a partial index'
    predicate and an expression index' key are not a column list, so the
    conflict scan cannot join on them -- but ``LIKE ... INCLUDING INDEXES``
    copies them to the twin verbatim.  A row whose *unfiltered* key matched one
    of those would be swallowed by ``ON CONFLICT DO NOTHING`` and then counted
    as a benign ``already_in_cold_rows``.  So the move refuses the table
    instead; the name is returned so the receipt can say which index.
    """
    if _regclass(conn, qualified) is None:
        return []
    rows = conn.execute(
        """
        SELECT i.indexrelid::regclass::text
        FROM pg_index i
        WHERE i.indrelid = %s::regclass AND i.indisunique AND i.indisvalid
          AND (i.indpred IS NOT NULL OR i.indexprs IS NOT NULL)
        ORDER BY 1
        """,
        (qualified,),
    ).fetchall()
    return [name for (name,) in rows]


def cutoff_index_state(conn, policy: TierPolicy) -> dict:
    """Our own cutoff index, looked up *by name* rather than by leading column.

    ``has_cutoff_index`` asks whether any valid index leads with the policy
    column, which is the question the planner cares about.  It is the wrong
    question for repair: a ``CREATE INDEX CONCURRENTLY`` that was cancelled
    leaves an index with ``indisvalid = false`` holding the name, which reads as
    "absent" there, while ``CREATE INDEX ... IF NOT EXISTS`` matches on the
    relation name regardless of validity and quietly does nothing.  Install
    would then report ``created`` forever without ever rebuilding it.
    """
    row = conn.execute(
        """
        SELECT i.indisvalid, i.indisready
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE i.indrelid = %s::regclass AND n.nspname = %s AND c.relname = %s
        """,
        (policy.qualified, policy.schema, policy.cutoff_index),
    ).fetchone()
    if row is None:
        return {"exists": False, "valid": None, "ready": None}
    return {"exists": True, "valid": bool(row[0]), "ready": bool(row[1])}


def has_cutoff_index(conn, policy: TierPolicy) -> bool:
    """Any index whose *leading* column is the policy column (not just ours)."""
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
            WHERE i.indrelid = %s::regclass AND i.indisvalid AND a.attname = %s
        )
        """,
        (policy.qualified, policy.column),
    ).fetchone()[0]
    return bool(row)


# --------------------------------------------------------------------------
# SQL builders (identifiers come from the frozen policy above)
# --------------------------------------------------------------------------


def _ident(name: str):
    from psycopg import sql

    schema, _, table = name.partition(".")
    return sql.Identifier(schema, table) if table else sql.Identifier(schema)


def _columns_sql(columns, prefix=None):
    from psycopg import sql

    if prefix is None:
        return sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    return sql.SQL(", ").join(sql.SQL("{}.{}").format(sql.Identifier(prefix), sql.Identifier(name))
                              for name in columns)


def _join_on(columns, left="b", right="c"):
    """``b.k = c.k AND ...`` -- plain ``=`` because that is what a unique index does.

    A NULL in a unique key never collides in PostgreSQL, so ``IS NOT DISTINCT
    FROM`` here would quarantine rows the twin would happily have accepted.
    """
    from psycopg import sql

    return sql.SQL(" AND ").join(
        sql.SQL("{l}.{c} = {r}.{c}").format(l=sql.Identifier(left), r=sql.Identifier(right), c=sql.Identifier(name))
        for name in columns
    )


def _chain_predicate(chain_columns):
    """``AND (c IS NULL OR c < %(chain_watermark)s)`` for every chain column.

    ``NULL`` never satisfies ``>= watermark``, so a row with no update stamp is
    inside the exported range as far as that column is concerned.
    """
    from psycopg import sql

    return sql.SQL("").join(
        sql.SQL(" AND ({c} IS NULL OR {c} < %(chain_watermark)s)").format(c=sql.Identifier(name))
        for name in chain_columns or ()
    )


def snapshot_batch_sql(policy: TierPolicy, columns, chain_columns=()):
    """Step 1: the batch, frozen into a TEMP table inside the move transaction.

    ``FOR UPDATE`` is what makes step 5 safe.  Without it the snapshot takes no
    row locks, so an ``UPDATE`` committed between the snapshot and the
    delete-by-primary-key is lost: the twin keeps the pre-update version and the
    ``DELETE`` removes the newer one.  These tables have upsert paths (the
    unique keys the conflict scan joins on), so that is a real sequence, not a
    theoretical one.  The batch is bounded (20 000 rows by default) and the
    transaction already carries ``lock_timeout = 30 s``, so a contended row
    fails the batch instead of queueing behind a writer.

    ``chain_columns`` clamps the batch to the incremental-backup chain: see
    :func:`parse_incremental_specs`.
    """
    from psycopg import sql

    return sql.SQL(
        "CREATE TEMPORARY TABLE tier_batch ON COMMIT DROP AS "
        "SELECT {cols} FROM {hot} WHERE {column} < %(cutoff)s{chain} "
        "ORDER BY {column} LIMIT %(batch)s FOR UPDATE"
    ).format(
        cols=_columns_sql(columns),
        hot=_ident(policy.qualified),
        column=sql.Identifier(policy.column),
        chain=_chain_predicate(chain_columns),
    )


def withheld_by_chain_sql(policy: TierPolicy, chain_columns):
    """How many rows past the cutoff the chain watermark is holding back.

    Bounded by ``%(probe)s`` so a table whose chain froze months ago costs one
    index range scan, not a count over millions of rows.
    """
    from psycopg import sql

    return sql.SQL(
        "SELECT count(*) FROM (SELECT 1 FROM {hot} WHERE {column} < %(cutoff)s "
        "AND NOT (TRUE{chain}) LIMIT %(probe)s) s"
    ).format(
        hot=_ident(policy.qualified),
        column=sql.Identifier(policy.column),
        chain=_chain_predicate(chain_columns),
    )


def conflict_scan_sql(policy: TierPolicy, pk_columns, unique_keys):
    """Step 2: the batch rows whose unique key is already in the twin with other content.

    ``to_jsonb(row)`` compares the whole row by column *name*, so a twin whose
    physical column order differs is still compared correctly; a twin whose
    column *set* differs is refused before we get here (schema drift).
    """
    from psycopg import sql

    branches = [
        sql.SQL(
            "SELECT {pk}, to_jsonb(b.*) AS hot_row, to_jsonb(c.*) AS cold_row "
            "FROM tier_batch b JOIN {cold} c ON {on} "
            "WHERE to_jsonb(b.*) IS DISTINCT FROM to_jsonb(c.*)"
        ).format(pk=_columns_sql(pk_columns, prefix="b"), cold=_ident(policy.cold_table), on=_join_on(key))
        for key in unique_keys
    ]
    return sql.SQL(
        "CREATE TEMPORARY TABLE tier_conflicts ON COMMIT DROP AS "
        "SELECT DISTINCT ON ({pk}) {pk}, hot_row, cold_row FROM ({branches}) s ORDER BY {pk}"
    ).format(pk=_columns_sql(pk_columns), branches=sql.SQL(" UNION ALL ").join(branches))


def quarantine_sql(pk_columns):
    """Step 3: the conflicting rows, preserved whole before they leave the hot table."""
    from psycopg import sql

    pk_json = sql.SQL(", ").join(
        sql.SQL("{}, q.{}").format(sql.Literal(name), sql.Identifier(name)) for name in pk_columns
    )
    return sql.SQL(
        "INSERT INTO {quarantine} (table_name, hot_pk, hot_row, cold_row) "
        "SELECT %(table)s, jsonb_build_object({pk}), q.hot_row, q.cold_row FROM tier_conflicts q"
    ).format(quarantine=_ident(QUARANTINE_TABLE), pk=pk_json)


def insert_batch_sql(policy: TierPolicy, columns, pk_columns):
    """Step 4: the non-conflicting rows, with an explicit column list (never ``*``)."""
    from psycopg import sql

    return sql.SQL(
        "INSERT INTO {cold} ({cols}) SELECT {b_cols} FROM tier_batch b "
        "WHERE NOT EXISTS (SELECT 1 FROM tier_conflicts q WHERE {on}) ON CONFLICT DO NOTHING"
    ).format(
        cold=_ident(policy.cold_table),
        cols=_columns_sql(columns),
        b_cols=_columns_sql(columns, prefix="b"),
        on=_join_on(pk_columns, left="b", right="q"),
    )


def delete_batch_sql(policy: TierPolicy, pk_columns):
    """Step 5: delete by primary key -- only after the twin holds the rows."""
    from psycopg import sql

    return sql.SQL("DELETE FROM {hot} h USING tier_batch b WHERE {on}").format(
        hot=_ident(policy.qualified), on=_join_on(pk_columns, left="h", right="b")
    )


def _set_local(conn, name: str, value: str):
    from psycopg import sql

    conn.execute(sql.SQL("SET LOCAL {} = {}").format(sql.Identifier(name), sql.Literal(value)))


def _set_session(conn, name: str, value: str):
    from psycopg import sql

    conn.execute(sql.SQL("SET {} = {}").format(sql.Identifier(name), sql.Literal(value)))


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
            "cutoff_index": policy.cutoff_index,
            "cutoff_index_present": None,
            # None = no index of that name; False = a cancelled CONCURRENTLY
            # build left an INVALID one, which the planner ignores and
            # CREATE INDEX ... IF NOT EXISTS will never replace.  `install`
            # repairs it (DROP INDEX CONCURRENTLY, then rebuild).
            "cutoff_index_valid": None,
            "size_bytes": None,
            "cold_size_bytes": None,
            "oldest_hot_at": None,
            "oldest_hot_days": None,
            "schema_drift": None,
        }
        if entry["exists"]:
            entry["size_bytes"] = _scalar(conn, "SELECT pg_total_relation_size(%s)", (policy.qualified,))
            entry["cutoff_index_present"] = has_cutoff_index(conn, policy)
            index_state = cutoff_index_state(conn, policy)
            entry["cutoff_index_valid"] = index_state["valid"]
            entry["unsupported_unique_indexes"] = unsupported_unique_indexes(conn, policy.qualified)
            oldest = _scalar(conn, _sql_min_column(policy), default=None)
            if oldest is not None:
                entry["oldest_hot_at"] = oldest.isoformat()
                entry["oldest_hot_days"] = max(0, int((now - oldest).total_seconds() // 86400))
            if with_counts:
                entry["hot_rows"], entry["hot_rows_basis"] = _count_rows(conn, policy.qualified)
        if entry["cold_exists"]:
            entry["cold_size_bytes"] = _scalar(conn, "SELECT pg_total_relation_size(%s)", (policy.cold_table,))
            entry["unsupported_unique_indexes"] = sorted(
                set(entry.get("unsupported_unique_indexes") or [])
                | set(unsupported_unique_indexes(conn, policy.cold_table))
            )
            if with_counts:
                entry["cold_rows"], entry["cold_rows_basis"] = _count_rows(conn, policy.cold_table)
        if entry["exists"] and entry["cold_exists"]:
            drift = schema_drift(table_columns(conn, policy.qualified), table_columns(conn, policy.cold_table))
            entry["schema_drift"] = drift if drift["blocking"] else None
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
    """Measured hot usage plus the budget the space policy may actually spend."""
    measured = measure_pgdata(settings["pgdata_dir"])
    configured = settings["budget_bytes"]
    budget = effective_budget(configured, measured["usage_bytes"], measured["volume_free_bytes"])
    effective = budget["effective_bytes"]
    report = {
        "pgdata_dir": str(settings["pgdata_dir"]),
        "configured_budget_bytes": configured,
        "effective_budget_bytes": effective,
        "budget_bytes": effective,
        "budget_fits_volume": budget["fits_volume"],
        "volume_capacity_bytes": budget["capacity_bytes"],
    }
    report.update(measured)
    report["usage_ratio"] = None if measured["usage_bytes"] is None else round(
        measured["usage_bytes"] / effective, 6
    )
    return report


def space_verdict(usage: dict, stats: dict, *, max_days_per_table: int = DEFAULT_MAX_SPACE_DAYS) -> dict:
    """Apply the pure space policy to the measured usage, or say why we cannot."""
    if not usage["measured"]:
        # An unmeasurable hot directory means the 500 GB cap is not being
        # enforced at all, which must never read as a green night.
        return {
            "status": "unknown",
            "alert": True,
            "steps": [],
            "table_hot_days": {},
            "reason": f"hot data directory not readable: {usage['pgdata_dir']}",
        }
    sizes = {name: entry["size_bytes"] or 0 for name, entry in stats.items() if entry["exists"]}
    oldest = {name: stats[name]["oldest_hot_days"] for name in sizes}
    # pg_wal is excluded from the tiering decision: moving evidence rows cannot
    # shrink it, and a backfill spike must not be read as permanent growth.
    verdict = plan_space_moves(
        usage["tiering_usage_bytes"],
        usage["budget_bytes"],
        sizes,
        oldest,
        max_days_per_table=max_days_per_table,
    )
    verdict["wal_bytes"] = usage.get("wal_bytes")
    verdict["excluded_reparse_points"] = usage.get("excluded_reparse_points", [])
    if usage.get("budget_fits_volume") is False:
        verdict["alert"] = True
        verdict["budget_exceeds_volume"] = True
        verdict["reason"] = (
            f"{verdict['reason']}; the configured budget "
            f"({usage['configured_budget_bytes']} bytes) does not fit the volume behind "
            f"{usage['pgdata_dir']} ({usage['volume_capacity_bytes']} bytes usable), so the effective "
            "budget was lowered to what the drive can deliver"
        )
    return verdict


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _lock_guarded(conn, note, action: str, target: str, run):
    """Run one DDL step; a lock we cannot take is recorded, not fatal.

    ``install`` takes AccessExclusiveLock on multi-gigabyte tables.  With a
    30 s ``lock_timeout`` a blocked step gives up and the rest of ``install``
    still completes, so the operator retries one target instead of the lot.
    """
    import psycopg

    try:
        return run()
    except (psycopg.errors.LockNotAvailable, psycopg.errors.QueryCanceled) as error:
        note(action, target, "skipped_locked", detail=_error_text(error))
        return None


def command_install(args, env) -> dict:
    from psycopg import sql

    settings = resolve_settings(args, env)
    policies = _policies(args)
    tablespace = settings["tablespace"]
    report = {
        "command": "install",
        "tablespace": tablespace,
        "tablespace_dir": str(settings["cold_dir"]),
        "lock_timeout_ms": LOCK_TIMEOUT_MS,
        "statement_timeout_ms": INSTALL_STATEMENT_TIMEOUT_MS,
        "actions": [],
        "errors": [],
        "skipped_locked": [],
        # Cutoff indexes that an interrupted CONCURRENTLY build left INVALID and
        # that this run could not rebuild; install finishes 'partial' for them.
        "invalid_indexes": [],
    }

    def note(action: str, target: str, result: str, **extra):
        report["actions"].append({"action": action, "target": target, "result": result, **extra})
        if result == "skipped_locked":
            report["skipped_locked"].append(target)
        if result == "invalid_index_present":
            report["invalid_indexes"].append(target)

    # CREATE TABLESPACE and CREATE INDEX CONCURRENTLY cannot run inside a
    # transaction block, hence the autocommit connection for the whole of
    # install; the timeouts are session settings for the same reason.
    with connect(env, autocommit=True, statement_timeout_ms=INSTALL_STATEMENT_TIMEOUT_MS) as conn:
        _set_session(conn, "lock_timeout", f"{LOCK_TIMEOUT_MS}ms")
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

        _install_quarantine_table(conn, note)

        for role, setting, value in ROLE_SETTINGS:
            if getattr(args, "skip_role_settings", False):
                note("alter_role", f"{role}.{setting}", "skipped_by_flag")
                continue
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                note("alter_role", f"{role}.{setting}", "role_missing")
                continue
            applied = _lock_guarded(
                conn, note, "alter_role", f"{role}.{setting}",
                lambda role=role, setting=setting, value=value: conn.execute(
                    sql.SQL("ALTER ROLE {} SET {} = {}").format(
                        sql.Identifier(role), sql.Identifier(setting), sql.Literal(value)
                    )
                ),
            )
            if applied is not None:
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

        # Cutoff indexes last: CONCURRENTLY is the slowest step and it must not
        # delay the twins, and a failure here only costs the job speed.
        _set_session(conn, "statement_timeout", f"{INDEX_STATEMENT_TIMEOUT_MS}ms")
        for policy in policies:
            try:
                _install_cutoff_index(conn, policy, note)
            except Exception as error:  # noqa: BLE001
                report["errors"].append({"target": policy.cutoff_index, "error": _error_text(error)})
        _set_session(conn, "statement_timeout", f"{INSTALL_STATEMENT_TIMEOUT_MS}ms")

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

    if report["errors"]:
        report["status"] = "failed"
    elif report["skipped_locked"] or report["invalid_indexes"]:
        report["status"] = "partial"
    else:
        report["status"] = "ok"
    return report


def _install_quarantine_table(conn, note):
    """The quarantine table, deliberately in the *default* (hot, backed up) tablespace."""
    from psycopg import sql

    created = _regclass(conn, QUARANTINE_TABLE) is None
    conn.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {quarantine} (
                conflict_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                table_name text NOT NULL,
                hot_pk jsonb NOT NULL,
                hot_row jsonb NOT NULL,
                cold_row jsonb NOT NULL,
                detected_at timestamptz NOT NULL DEFAULT now()
            )
            """
        ).format(quarantine=_ident(QUARANTINE_TABLE))
    )
    conn.execute(
        sql.SQL("CREATE INDEX IF NOT EXISTS storage_tier_conflicts_table_detected_idx ON {quarantine} "
                "(table_name, detected_at DESC)").format(quarantine=_ident(QUARANTINE_TABLE))
    )
    note("create_quarantine_table", QUARANTINE_TABLE, "created" if created else "already_exists")


def _install_cutoff_index(conn, policy: TierPolicy, note):
    """``CREATE INDEX CONCURRENTLY`` on the policy column (autocommit connection).

    Migration 20260919_0106 creates the same five indexes with the same names so
    a database rebuilt from the chain already has them; ``IF NOT EXISTS`` makes
    whichever runs second a no-op.  Repairing a *cancelled* build is this
    function's job and nobody else's (the migration's docstring says so): a
    ``CREATE INDEX CONCURRENTLY`` that is interrupted -- and on the 19 GB
    ``quant.raw_market_observations`` an interruption is the likely first
    outcome -- leaves an index with ``indisvalid = false`` holding the name.
    The planner ignores it, ``IF NOT EXISTS`` matches it and does nothing, and
    the previous version of this function reported ``created`` while nothing had
    been built.  So: look the name up, drop an invalid one CONCURRENTLY, rebuild.
    """
    from psycopg import sql

    if _regclass(conn, policy.qualified) is None:
        note("create_cutoff_index", policy.cutoff_index, "hot_table_missing")
        return
    state = cutoff_index_state(conn, policy)
    if state["exists"] and not state["valid"]:
        dropped = _lock_guarded(
            conn, note, "drop_invalid_cutoff_index", policy.cutoff_index,
            lambda: conn.execute(
                sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(
                    sql.Identifier(policy.schema, policy.cutoff_index)
                )
            ),
        )
        if dropped is None:
            # The drop needs a ShareUpdateExclusiveLock it could not take.  The
            # table still has no usable cutoff index, so say exactly that and
            # let install finish 'partial' rather than 'ok'.
            note(
                "create_cutoff_index",
                policy.cutoff_index,
                "invalid_index_present",
                detail="an INVALID index holds the name and DROP INDEX CONCURRENTLY could not take "
                "its lock; re-run install when the table is quiet",
            )
            return
        done = _lock_guarded(
            conn, note, "create_cutoff_index", policy.cutoff_index,
            lambda: conn.execute(
                sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {hot} ({column})").format(
                    name=sql.Identifier(policy.cutoff_index),
                    hot=_ident(policy.qualified),
                    column=sql.Identifier(policy.column),
                )
            ),
        )
        if done is None:
            return
        # Read back for the same reason the fresh-build path below does: a
        # CONCURRENTLY build that fails after its second pass leaves an INVALID
        # index without raising here, and on the 19 GB table that is the likely
        # outcome twice in a row.  Reporting 'rebuilt_invalid' on a rebuild that
        # produced another invalid index would tell the operator the repair
        # worked when the table still has no usable cutoff index.
        rebuilt = cutoff_index_state(conn, policy)
        if not rebuilt["exists"] or not rebuilt["valid"]:
            note(
                "create_cutoff_index",
                policy.cutoff_index,
                "invalid_index_present",
                detail="the rebuild left an INVALID index; the next install drops and rebuilds it again",
            )
            return
        note("create_cutoff_index", policy.cutoff_index, "rebuilt_invalid")
        return
    if state["exists"] or has_cutoff_index(conn, policy):
        note("create_cutoff_index", policy.cutoff_index, "already_present")
        return
    done = _lock_guarded(
        conn, note, "create_cutoff_index", policy.cutoff_index,
        lambda: conn.execute(
            sql.SQL("CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {hot} ({column})").format(
                name=sql.Identifier(policy.cutoff_index),
                hot=_ident(policy.qualified),
                column=sql.Identifier(policy.column),
            )
        ),
    )
    if done is None:
        return
    # A CONCURRENTLY build that fails *after* its second pass leaves the index
    # invalid without raising here, so the claim is read back rather than assumed.
    after = cutoff_index_state(conn, policy)
    if after["exists"] and not after["valid"]:
        note(
            "create_cutoff_index",
            policy.cutoff_index,
            "invalid_index_present",
            detail="the CONCURRENTLY build left an INVALID index; the next install rebuilds it",
        )
        return
    note("create_cutoff_index", policy.cutoff_index, "created")


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

    hot_columns = table_columns(conn, policy.qualified)
    drift = schema_drift(hot_columns, table_columns(conn, policy.cold_table))
    if drift["repairable"]:
        # The hot table gained columns (an Alembic migration); reconcile the twin
        # rather than letting the next apply write into the wrong columns.  The
        # copy is nullable and keeps no NOT NULL: the rows already in the twin
        # predate the column and have nothing to put there.
        types = dict(hot_columns)
        for column in drift["missing_in_cold"]:
            _lock_guarded(
                conn, note, "reconcile_cold_twin", f"{policy.cold_table}.{column}",
                lambda column=column: conn.execute(
                    sql.SQL("ALTER TABLE {cold} ADD COLUMN IF NOT EXISTS {column} ").format(
                        cold=_ident(policy.cold_table), column=sql.Identifier(column)
                    ) + sql.SQL(types[column])  # noqa: S608 - format_type output from pg_attribute
                ),
            )
        note("reconcile_cold_twin", policy.cold_table, "columns_added", columns=drift["missing_in_cold"])
        drift = schema_drift(hot_columns, table_columns(conn, policy.cold_table))
    if drift["blocking"]:
        # Not repairable in place (the twin has a column the hot table lost, or
        # a type moved underneath).  Say so loudly; `apply` refuses this table.
        note("reconcile_cold_twin", policy.cold_table, "schema_drift", detail=json.dumps(drift, default=str))

    moved_indexes = _move_indexes_to_cold(conn, policy.cold_table, tablespace, note)
    if moved_indexes:
        note("move_indexes", policy.cold_table, "moved", indexes=moved_indexes)

    if drift["blocking"]:
        note("create_view", policy.all_view, "skipped_schema_drift")
    else:
        _create_all_view(conn, policy, [name for name, _ in hot_columns], note)


def _create_all_view(conn, policy: TierPolicy, columns, note):
    """``SELECT <explicit columns>`` -- never ``*``, which PostgreSQL freezes at creation.

    ``CREATE OR REPLACE VIEW`` refuses to change a view's column list, so a view
    whose shape moved is dropped and rebuilt.  Nothing in ``app/`` may depend on
    it (guard test), so dropping it is safe.
    """
    from psycopg import sql
    import psycopg

    statement = sql.SQL(
        "CREATE OR REPLACE VIEW {view} AS SELECT {cols} FROM {hot} UNION ALL SELECT {cols} FROM {cold}"
    ).format(
        view=_ident(policy.all_view),
        cols=_columns_sql(columns),
        hot=_ident(policy.qualified),
        cold=_ident(policy.cold_table),
    )
    try:
        conn.execute(statement)
    except psycopg.Error:
        conn.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(_ident(policy.all_view)))
        conn.execute(statement)
        note("create_view", policy.all_view, "recreated", columns=list(columns))
        return
    note("create_view", policy.all_view, "applied", columns=list(columns))


def _move_indexes_to_cold(conn, qualified: str, tablespace: str, note) -> list[str]:
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
        done = _lock_guarded(
            conn, note, "move_index", f"{schema}.{index_name}",
            lambda schema=schema, index_name=index_name: conn.execute(
                sql.SQL("ALTER INDEX {} SET TABLESPACE {}").format(
                    sql.Identifier(schema, index_name), sql.Identifier(tablespace)
                )
            ),
        )
        if done is not None:
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
        done = _lock_guarded(
            conn, note, "move_table_to_cold", qualified,
            lambda: conn.execute(
                sql.SQL("ALTER TABLE {} SET TABLESPACE {}").format(_ident(qualified), sql.Identifier(tablespace))
            ),
        )
        if done is None:
            return
        note("move_table_to_cold", qualified, "moved")
    moved_indexes = _move_indexes_to_cold(conn, qualified, tablespace, note)
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
        report["space_policy"] = space_verdict(usage, stats, max_days_per_table=settings["max_space_days"])
        _note_installation(conn, report, settings)
    report["status"] = _read_only_status(report)
    return report


def _note_installation(conn, report: dict, settings) -> None:
    """Record whether ``install`` has actually run, for ``plan`` and ``status``.

    Without the tablespace or the quarantine table ``apply`` moves nothing at
    all, so a read-only command that answers "ok" is describing a budget guard
    that is not guarding anything.
    """
    report["tablespace_installed"] = bool(
        conn.execute(
            "SELECT 1 FROM pg_tablespace WHERE spcname = %s", (settings["tablespace"],)
        ).fetchone()
    )
    report["quarantine_table_installed"] = _regclass(conn, QUARANTINE_TABLE) is not None


def _read_only_status(report: dict) -> str:
    """``plan``/``status`` verdict: degraded (exit 2) when the guard cannot run."""
    if report["space_policy"]["status"] in DEGRADED_SPACE_STATUSES:
        return "degraded"
    missing = []
    if report.get("tablespace_installed") is False:
        missing.append("the cold tablespace")
    if report.get("quarantine_table_installed") is False:
        missing.append(QUARANTINE_TABLE)
    if any(entry.get("exists") and not entry.get("cold_exists") for entry in report.get("tables", [])):
        missing.append("at least one cold twin")
    if missing:
        report["not_installed"] = missing
        report["reason"] = (
            "install has not run: " + ", ".join(missing) + " is missing, so apply would move no rows "
            "and the hot budget is unguarded"
        )
        return "degraded"
    return "ok"


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
    deadline = resolve_deadline(
        datetime.now().astimezone(), getattr(args, "deadline", None), getattr(args, "max_seconds", None)
    )
    usage_before = usage_report(settings)
    record = {
        "command": "apply",
        "started_at": started_at.isoformat(),
        "batch_rows": settings["batch_rows"],
        "max_space_days": settings["max_space_days"],
        "deadline": None if deadline is None else deadline.isoformat(),
        "deadline_reached": False,
        # The deadline was already behind us when the run started: nothing was
        # attempted at all.  A machine that was off at 06:00 and comes up on a
        # Saturday at 10:00 hits exactly this, and it must not be filed next to
        # a run that worked until 08:00.
        "deadline_missed": False,
        "usage_before": usage_before,
        "tables": [],
        "errors": [],
    }
    if _deadline_passed(deadline):
        record["deadline_missed"] = True
        record["deadline_missed_reason"] = (
            f"the deadline {record['deadline']} was already past when the run started "
            f"({started_at.isoformat()}); no table was attempted"
        )
    with connect(env, autocommit=True, statement_timeout_ms=settings["statement_timeout_ms"]) as conn:
        stats_before = table_stats(conn, policies, now=started_at)
        record["space_policy_before"] = space_verdict(
            usage_before, stats_before, max_days_per_table=settings["max_space_days"]
        )

        results: dict[str, dict] = {}
        for policy in policies:
            if _deadline_passed(deadline):
                record["deadline_reached"] = True
                break
            moved = _move_table(
                conn, policy, started_at, policy.hot_days, settings, record, deadline=deadline
            )
            # Re-measure after every table: the space verdict below must see what
            # these moves actually did to the directory, not an estimate.
            moved["usage_after_bytes"] = usage_report(settings)["usage_bytes"]
            results[policy.qualified] = moved

        usage_mid = usage_report(settings)
        stats_mid = table_stats(conn, policies, now=started_at)
        verdict = space_verdict(usage_mid, stats_mid, max_days_per_table=settings["max_space_days"])
        record["usage_after_time_moves"] = usage_mid
        record["space_policy"] = verdict
        _run_space_ratchet(conn, policies, verdict, results, started_at, settings, record, deadline)

        record["tables"] = [results[policy.qualified] for policy in policies if policy.qualified in results]
        record["usage_after"] = usage_report(settings)

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["moved_rows"] = sum(entry["deleted_rows"] for entry in record["tables"])
    record["quarantined_rows"] = sum(entry.get("quarantined_rows", 0) for entry in record["tables"])
    record["status"] = _apply_status(record)
    table_statuses = {entry.get("status") for entry in record["tables"]}
    record["alert"] = bool(
        record["space_policy"].get("alert")
        or record["space_policy_before"].get("alert")
        or record["quarantined_rows"]
        or record["deadline_missed"]
        # chain_behind is a working state (the clamp did its job), but a chain
        # that is behind enough to withhold rows is an operator problem.
        or table_statuses & (BLOCKING_TABLE_STATUSES | NOT_INSTALLED_TABLE_STATUSES | {"chain_behind"})
        or record["status"] in ("degraded", "schema_drift", "partial", "deadline_missed")
    )
    _append_jsonl(settings["log_file"], record)
    return record


def _apply_status(record) -> str:
    """Most severe wins: degraded > schema_drift > partial > conflicts > deadline > ok.

    ``schema_drift`` outranks ``partial`` because it names the one failure an
    operator must act on before the next run: every other per-table failure is
    retried harmlessly tomorrow, a drifted twin is not.

    Two of these rungs exist because a green scheduled task is the most
    expensive kind of wrong:

    * ``deadline_missed`` -- the deadline was already behind us before the first
      table, so nothing was even attempted.  ``deadline_reached`` (exit 0) means
      "worked until the window closed"; this one means "the window was shut",
      and the two must not look the same in the log.
    * ``partial`` for any ``skipped_missing_table`` / ``skipped_missing_quarantine``
      -- those mean ``install`` has not run, which is exactly today's production
      state, and a run that moved nothing at all is not a success.
    """
    statuses = {entry.get("status") for entry in record["tables"]}
    status = "ok"
    if record.get("deadline_reached") or "deadline_reached" in statuses:
        status = "deadline_reached"
    if record.get("deadline_missed"):
        status = "deadline_missed"
    if record.get("quarantined_rows"):
        status = "conflicts"
    if record["errors"] or statuses & NOT_INSTALLED_TABLE_STATUSES or statuses & BLOCKING_TABLE_STATUSES:
        status = "partial"
    if "schema_drift" in statuses:
        status = "schema_drift"
    if record["space_policy"].get("status") in DEGRADED_SPACE_STATUSES:
        status = "degraded"
    return status


def _deadline_passed(deadline) -> bool:
    return deadline is not None and datetime.now(timezone.utc) >= deadline


def _run_space_ratchet(conn, policies, verdict, results, started_at, settings, record, deadline):
    """Take at most ``max_space_days`` days per table, re-measuring after each one.

    Plain VACUUM cannot give the pages back to the filesystem, so the measured
    directory usually does not shrink at all.  That is expected -- the freed
    pages are reused by new inserts, which is what bounds growth -- but it means
    a naive "keep cutting until usage falls" loop would eat the whole hot window
    in one night.  When the measurement does not fall by at least
    ``MIN_USAGE_DROP_RATIO`` after rows actually moved, the ratchet stops with
    ``needs_repack``: reclaiming existing bloat needs VACUUM FULL or pg_repack
    in a maintenance window, and this job never runs either by itself.
    """
    by_name = {policy.qualified: policy for policy in policies}
    steps = verdict.get("table_hot_days", {})
    ratchet = {"status": verdict.get("status"), "tables": []}
    record["space_ratchet"] = ratchet
    for name, hot_days in sorted(steps.items()):
        policy = by_name.get(name)
        if policy is None:
            continue
        if _deadline_passed(deadline):
            record["deadline_reached"] = True
            ratchet["status"] = "deadline_reached"
            break
        before = usage_report(settings)
        extra = _move_table(
            conn, policy, started_at, hot_days, settings, record, reason="space_policy", deadline=deadline
        )
        after = usage_report(settings)
        merged = results.get(name)
        if merged is None:
            results[name] = extra
        else:
            merged["deleted_rows"] += extra["deleted_rows"]
            merged["inserted_rows"] += extra["inserted_rows"]
            merged["quarantined_rows"] = merged.get("quarantined_rows", 0) + extra.get("quarantined_rows", 0)
            merged["batches"] += extra["batches"]
            merged["space_policy_hot_days"] = hot_days
            if extra["status"] not in ("ok",):
                merged["status"] = extra["status"]
        # Measured on tiering_usage_bytes -- the WAL-free quantity the policy is
        # expressed in and that space_verdict feeds plan_space_moves.  Against
        # total usage_bytes the drop was routinely *negative*: moving a week of
        # a 19 GB table is millions of DELETE+INSERT rows plus a VACUUM, which
        # writes gigabytes of WAL into the same directory, so pg_wal grew by more
        # than the heap could ever give back and the ratchet stopped with
        # 'needs_repack' for a reason that had nothing to do with repacking.
        # wal_before/wal_after are in the receipt so that is visible rather than
        # inferred.
        dropped = None
        tiering_before = before.get("tiering_usage_bytes")
        tiering_after = after.get("tiering_usage_bytes")
        if before["measured"] and after["measured"] and (tiering_before or 0) > 0:
            dropped = (tiering_before - tiering_after) / tiering_before
        ratchet["tables"].append(
            {
                "table": name,
                "hot_days": hot_days,
                "moved_rows": extra["deleted_rows"],
                "usage_before_bytes": before["usage_bytes"],
                "usage_after_bytes": after["usage_bytes"],
                "tiering_usage_before_bytes": tiering_before,
                "tiering_usage_after_bytes": tiering_after,
                "wal_before_bytes": before.get("wal_bytes"),
                "wal_after_bytes": after.get("wal_bytes"),
                "usage_drop_ratio": None if dropped is None else round(dropped, 6),
            }
        )
        if extra["deleted_rows"] > 0 and (dropped is None or dropped < MIN_USAGE_DROP_RATIO):
            ratchet["status"] = "needs_repack"
            record["space_policy"]["status"] = "needs_repack"
            record["space_policy"]["alert"] = True
            record["space_policy"]["reason"] = (
                f"moved {extra['deleted_rows']} rows out of {name} and the hot directory's non-WAL "
                f"usage did not fall by {MIN_USAGE_DROP_RATIO:.0%} "
                f"({tiering_before} -> {tiering_after} bytes, WAL {before.get('wal_bytes')} -> "
                f"{after.get('wal_bytes')}); plain VACUUM cannot return front-of-heap pages to the "
                "filesystem, so the ratchet stopped. New inserts will reuse the freed pages (growth is "
                "bounded), but reclaiming the existing bloat needs VACUUM FULL or pg_repack in a "
                "maintenance window, or more disk. This job never runs either by itself."
            )
            break


def _chain_guard(policy: TierPolicy, hot_columns, settings, result: dict, record: dict):
    """Clamp one table's move to its incremental-backup chain, or refuse it.

    The nightly ``pg_dump`` excludes the data of every table in
    ``STOCK_BACKUP_INCREMENTAL_TABLES`` *and* the data of its ``_cold`` twin,
    because the chunk chain under ``backups\\incremental\\<table>\\`` is what
    holds their history.  That is only true for rows the chain has actually
    exported.  The chain's upper bound lags ``now()`` by 30 minutes and the
    04:10 run is two hours before this job, so a row created after ~03:40 and
    moved at 06:00 would be in the twin -- excluded from the dump -- with no
    chunk behind it.  Nothing would ever notice until a restore.

    Returns ``(chain_columns, params)`` for the snapshot predicate, or ``None``
    when the caller must stop: a table whose chain state cannot be read moves
    nothing at all, because "no watermark" and "the chain is up to date" are
    indistinguishable from here and only one of them is safe.
    """
    specs = settings.get("incremental_specs") or {}
    spec = specs.get(policy.qualified.lower())
    result["chain_protected"] = spec is not None
    if spec is None:
        return ((), {})
    created_column, updated_column = spec
    present = {name for name in hot_columns}
    absent = [name for name in (created_column, updated_column) if name and name not in present]
    if absent:
        # The env file names a column this table does not have, so the clamp
        # cannot be built.  Refusing beats moving unclamped rows out of a table
        # whose twin the dump excludes.
        result["status"] = "chain_columns_missing"
        result["detail"] = (
            f"STOCK_BACKUP_INCREMENTAL_TABLES names column(s) {absent} that {policy.qualified} "
            "does not have, so the chain clamp cannot be built"
        )
        record["errors"].append({"table": policy.qualified, "error": result["detail"]})
        return None
    state = read_chain_state(settings.get("backup_root"), policy.qualified)
    result["chain_state_file"] = state["state_file"]
    if state["watermark"] is None:
        result["status"] = "chain_missing"
        result["detail"] = state["reason"]
        record["errors"].append(
            {"table": policy.qualified, "error": f"chain watermark unavailable: {state['reason']}"}
        )
        return None
    result["chain_watermark"] = state["watermark"].isoformat()
    columns = tuple(name for name in (created_column, updated_column) if name)
    return (columns, {"chain_watermark": state["watermark"]})


def _move_table(conn, policy: TierPolicy, now, hot_days: int, settings, record,
                reason: str = "hot_window", deadline=None) -> dict:
    """Move everything older than the cutoff, one bounded batch per transaction.

    Every batch is insert-first-then-delete-by-primary-key inside one
    transaction (see the module docstring), so a kill at any instant loses
    nothing and duplicates nothing.
    """
    cutoff = hot_cutoff(now, hot_days)
    result = {
        "table": policy.qualified,
        "column": policy.column,
        "hot_days": int(hot_days),
        "cutoff": cutoff.isoformat(),
        "reason": reason,
        "deleted_rows": 0,
        "inserted_rows": 0,
        "quarantined_rows": 0,
        "batches": 0,
        "status": "ok",
    }
    try:
        if _regclass(conn, policy.qualified) is None or _regclass(conn, policy.cold_table) is None:
            result["status"] = "skipped_missing_table"
            return result
        if _regclass(conn, QUARANTINE_TABLE) is None:
            result["status"] = "skipped_missing_quarantine"
            result["detail"] = f"{QUARANTINE_TABLE} is missing; run install first"
            return result

        hot_columns = table_columns(conn, policy.qualified)
        drift = schema_drift(hot_columns, table_columns(conn, policy.cold_table))
        if drift["blocking"]:
            # Writing a hot row into a twin whose columns moved would put values
            # in the wrong place, and no receipt would say so.
            result["status"] = "schema_drift"
            result["schema_drift"] = drift
            record["errors"].append({"table": policy.qualified, "error": f"schema drift: {drift}"})
            return result
        columns = [name for name, _ in hot_columns]
        pk_columns = primary_key_columns(conn, policy.qualified)
        if not pk_columns:
            result["status"] = "skipped_no_primary_key"
            return result
        unsupported = sorted(
            set(unsupported_unique_indexes(conn, policy.qualified))
            | set(unsupported_unique_indexes(conn, policy.cold_table))
        )
        if unsupported:
            # The conflict scan joins on column lists; a partial or expression
            # unique index has neither, so the twin could silently swallow a
            # differing row through ON CONFLICT DO NOTHING and this job would
            # count it as a benign 'already_in_cold_rows'.
            result["status"] = "unsupported_unique_index"
            result["unsupported_unique_indexes"] = unsupported
            result["detail"] = (
                "a partial or expression unique index exists on the hot table or its twin; the "
                "conflict scan cannot join on it, so the move is refused rather than risking a "
                "silently dropped row"
            )
            record["errors"].append(
                {"table": policy.qualified, "error": f"unsupported unique index(es): {unsupported}"}
            )
            return result

        chain = _chain_guard(policy, columns, settings, result, record)
        if chain is None:
            return result
        chain_columns, chain_params = chain
        unique_keys = unique_key_columns(conn, policy.cold_table) or [tuple(pk_columns)]

        snapshot = snapshot_batch_sql(policy, columns, chain_columns)
        scan = conflict_scan_sql(policy, pk_columns, unique_keys)
        quarantine = quarantine_sql(pk_columns)
        insert = insert_batch_sql(policy, columns, pk_columns)
        delete = delete_batch_sql(policy, pk_columns)
        max_batches = int(settings.get("max_batches") or 0)
        while True:
            if _deadline_passed(deadline):
                result["status"] = "deadline_reached"
                record["deadline_reached"] = True
                break
            with conn.transaction():
                _set_local(conn, "statement_timeout", str(settings["statement_timeout_ms"]))
                _set_local(conn, "lock_timeout", str(LOCK_TIMEOUT_MS))
                conn.execute(
                    snapshot, {"cutoff": cutoff, "batch": settings["batch_rows"], **chain_params}
                )
                batch_rows = conn.execute("SELECT count(*) FROM tier_batch").fetchone()[0]
                if batch_rows:
                    conn.execute(scan)
                    quarantined = conn.execute("SELECT count(*) FROM tier_conflicts").fetchone()[0]
                    if quarantined:
                        conn.execute(quarantine, {"table": policy.qualified})
                    inserted = conn.execute(insert).rowcount
                    deleted = conn.execute(delete).rowcount
                else:
                    quarantined = inserted = deleted = 0
            result["deleted_rows"] += int(deleted)
            result["inserted_rows"] += int(inserted)
            result["quarantined_rows"] += int(quarantined)
            result["batches"] += 1
            if int(batch_rows) == 0:
                break
            if max_batches and result["batches"] >= max_batches:
                result["status"] = "batch_limit_reached"
                break
        skipped = result["deleted_rows"] - result["inserted_rows"] - result["quarantined_rows"]
        if skipped > 0:
            # Byte-identical rows the twin already held: a resumed run.
            result["already_in_cold_rows"] = skipped
        if result["quarantined_rows"] and result["status"] == "ok":
            result["status"] = "conflicts"
        if chain_columns and result["status"] in ("ok", "conflicts"):
            # The loop ended because no *unclamped* row was left.  Anything still
            # older than the cutoff is being held back by the chain watermark:
            # name it, because the move is silently doing less than the receipt's
            # cutoff implies, and a chain that froze weeks ago is an operator problem.
            # Deliberately NOT through _scalar: its swallow-to-None turns a
            # probe that timed out into the same answer as a probe that found
            # nothing, and those are opposite findings.  "Nothing withheld"
            # means the move finished the cutoff; "could not tell" means the
            # receipt cannot claim that, so the clamp is reported as unknown.
            import psycopg

            try:
                with conn.transaction():
                    row = conn.execute(
                        withheld_by_chain_sql(policy, chain_columns),
                        {"cutoff": cutoff, "probe": CHAIN_PROBE_ROWS, **chain_params},
                    ).fetchone()
                withheld = 0 if row is None else int(row[0] or 0)
            except psycopg.Error as error:
                result["chain_withheld_rows"] = None
                result["detail"] = (
                    "the move stopped at the incremental backup chain's watermark "
                    f"({result.get('chain_watermark')}) and the probe for how many rows that "
                    f"withholds could not be answered ({_error_text(error)}); the count is "
                    "unknown, not zero"
                )
                # Same precedence as the answered case: 'conflicts' is the more
                # urgent finding and keeps the status.  Everything else becomes
                # chain_behind, because a clamp that cannot be measured is still
                # a clamp and the receipt must not read as a finished cutoff.
                if result["status"] == "ok":
                    result["status"] = "chain_behind"
                withheld = 0
            if withheld:
                result["chain_withheld_rows"] = int(withheld)
                result["chain_withheld_rows_capped"] = int(withheld) >= CHAIN_PROBE_ROWS
                result["detail"] = (
                    f"{int(withheld)}+ row(s) older than the cutoff stay hot because the "
                    f"incremental backup chain has only exported up to "
                    f"{result.get('chain_watermark')}; moving them would put them in a "
                    "dump-excluded twin with no chunk behind them"
                )
                # 'conflicts' is the more urgent finding and keeps the status;
                # chain_withheld_rows carries the clamp on its own in that case.
                if result["status"] == "ok":
                    result["status"] = "chain_behind"
    except Exception as error:  # noqa: BLE001 - per-table isolation is the contract
        result["status"] = "failed"
        result["error"] = _error_text(error)
        record["errors"].append({"table": policy.qualified, "error": result["error"]})
    _vacuum_after_move(conn, policy, settings, result)
    return result


def _vacuum_after_move(conn, policy: TierPolicy, settings, result: dict):
    """``VACUUM (ANALYZE)`` with its own ceiling, outside the move's try.

    The rows are already in the twin and already gone from the hot table when
    this runs, so the move's correctness does not depend on it -- but it was
    issued on a connection carrying the 10-minute *batch* ``statement_timeout``,
    and the first drain of a 19 GB table with six indexes takes longer than
    that.  A timeout there turned a completely successful move into
    ``failed`` for the table and ``partial`` for the whole run.  Now it gets
    30 minutes of its own and is recorded as ``vacuum: timed_out``.
    """
    import psycopg
    from psycopg import sql

    if result["deleted_rows"] <= 0:
        return
    try:
        _set_session(conn, "statement_timeout", f"{VACUUM_STATEMENT_TIMEOUT_MS}ms")
        try:
            # ANALYZE keeps the planner honest; VACUUM lets the freed pages be
            # reused by new inserts.  Neither returns bytes to the filesystem.
            conn.execute(sql.SQL("VACUUM (ANALYZE) {}").format(_ident(policy.qualified)))
            result["vacuum"] = "ok"
            result["vacuumed"] = True
        finally:
            # SET, not SET LOCAL, on both sides -- and that is why the restore
            # has to be explicit.  VACUUM cannot run inside a transaction block,
            # so this connection is in autocommit and there is no transaction
            # for SET LOCAL to be scoped to (it would warn and change nothing).
            # A plain SET therefore lasts for the whole session, so the 30-minute
            # ceiling raised above would silently outlive this table and follow
            # the batch loop of every later one.  The batches use _set_local
            # inside their own transaction precisely because they must not.
            _set_session(conn, "statement_timeout", f"{int(settings['statement_timeout_ms'])}ms")
    except psycopg.errors.QueryCanceled:
        result["vacuum"] = "timed_out"
        result["vacuumed"] = False
    except Exception as error:  # noqa: BLE001 - a vacuum never fails the move
        result["vacuum"] = "failed"
        result["vacuum_error"] = _error_text(error)
        result["vacuumed"] = False


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
        report["quarantine_table_installed"] = _regclass(conn, QUARANTINE_TABLE) is not None
        report["quarantined_rows"] = (
            _scalar(conn, f"SELECT count(*) FROM {QUARANTINE_TABLE}")
            if report["quarantine_table_installed"]
            else None
        )
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
        report["space_policy"] = space_verdict(usage, stats, max_days_per_table=settings["max_space_days"])
    report["status"] = _read_only_status(report)
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


def exit_code_for(status) -> int:
    return EXIT_CODES.get(status, 1)


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
    common.add_argument(
        "--max-space-days",
        type=int,
        help=f"space policy: most days of history one table may give up per run (default {DEFAULT_MAX_SPACE_DAYS})",
    )
    common.add_argument(
        "--deadline",
        help="local wall-clock HH:MM at which apply stops between batches and writes its receipt "
        "(default none); the scheduled 06:00 run passes 08:00",
    )
    common.add_argument("--max-seconds", type=int, help="relative deadline in seconds; the earlier of the two wins")
    common.add_argument("--log-file", help=f"apply run log (default {DEFAULT_LOG_FILE})")
    common.add_argument(
        "--backup-root",
        help="backup root holding incremental\\<table>\\state.json; the move of a table whose twin "
        f"the nightly dump excludes is clamped to that watermark (default STOCK_BACKUP_ROOT or "
        f"{DEFAULT_BACKUP_ROOT})",
    )

    parser = argparse.ArgumentParser(
        prog="database-storage-tiers",
        description="Owner PostgreSQL hot/cold storage tiers: install, plan, apply, status.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser(
        "install", parents=[common], help="create the cold tablespace, twins, views and role timeouts"
    )
    install.add_argument(
        "--skip-role-settings",
        action="store_true",
        help="do not issue the ALTER ROLE statements. They are cluster-wide, not per-database, so an "
        "install exercised against a scratch database would otherwise reach into the live cluster.",
    )
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
        return EXIT_CODES["failed"]
    print(json.dumps(report, ensure_ascii=True, default=str))
    return exit_code_for(report.get("status"))


if __name__ == "__main__":
    raise SystemExit(main())
