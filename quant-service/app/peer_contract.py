"""The machine-readable description of what an external consumer may rely on.

The 2026-09-19 peer outage was caused by a startup gate asserting a schema that
had been *inferred from prose*: a handoff document described ``adjustment_state``
as a four-state value computed by the owner health panel, and the consumer
turned it into a required column on three tables; it also hardcoded a factor
semantics value (``cumulative_tushare``) the owner has never written.  Neither
mistake is detectable by reading a document more carefully -- prose invites
completion by inference, and the only cure is to publish the real thing.

So this module introspects the live cluster and returns it.  Two halves matter
equally:

``objects``
    What exists, with its real columns, nullability, indexes and tablespace.
    Assertions may be written against these and nothing else.

``not_provided``
    What does *not* exist and will not be added, named explicitly, so a
    consumer that inferred it can delete the check instead of waiting for a
    migration that is never coming.

``derived_rules``
    Questions whose answer is not a column.  v1 published where factor
    semantics live and stopped there, which was the same defect one level in:
    on 2026-09-20 only 2.3% of ``daily_adjustment_factors`` rows carried the
    key at all, 61,614 rows carried a ``superseded_at`` marker the contract
    never mentioned, and whether a factor may back a bar depends on the
    provider as much as on the semantics.  A consumer reading the enumeration
    alone had no way to reach the owner's own answer, so the owner's predicate
    is published verbatim instead of described.

The allowlist below is a deliberate curation, not everything the peer role can
read: ``stock_peer`` inherits ``quant_app`` and can therefore SELECT more than
200 relations, almost none of which are part of any agreement.  Publishing all
of them would recreate the original problem one level up, by implying support
for whatever happens to be reachable today.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .tushare_normalization import (
    CUMULATIVE_FACTOR_SEMANTICS,
    DERIVED_FACTOR_PROVIDER,
    PROMOTABLE_FACTOR_PROVIDER_PREFIX,
    PROMOTABLE_FACTOR_SEMANTICS,
    SUPERSEDED_MARKER,
    promotable_factor_evidence_sql,
)

CONTRACT_VERSION = "peer-contract-v2"

SCHEMA = "quant"


@dataclass(frozen=True)
class SupportedObject:
    """One relation an external consumer is allowed to depend on."""

    name: str
    purpose: str
    access: str  # "read" or "read-write"


#: Everything outside this list is reachable by accident, not by agreement.
SUPPORTED_OBJECTS: tuple[SupportedObject, ...] = (
    SupportedObject("canonical_bars_daily", "settled daily bars, the single price source", "read"),
    SupportedObject("daily_adjustment_factors", "adjustment factors; semantics live in raw->>'factor_semantics'", "read"),
    SupportedObject("daily_fundamentals", "per-symbol daily fundamentals", "read"),
    SupportedObject("daily_trade_limits", "per-symbol limit-up/limit-down prices", "read"),
    SupportedObject("security_suspensions", "trading suspensions", "read"),
    SupportedObject("legacy_source_records", "archived source rows; lives in the stock_cold tablespace", "read"),
    SupportedObject("alembic_version", "owner migration head; pin checks against this", "read"),
    SupportedObject("intraday_board_flow_snapshots", "intraday board flow snapshots written by the peer collector", "read-write"),
    SupportedObject("runtime_leases", "background-task leases written by the peer scheduler", "read-write"),
    SupportedObject("owner_deploy_events", "owner deployment announcements; the only channel a release does not restart", "read"),
)

COLD_TABLESPACE = "stock_cold"


@dataclass(frozen=True)
class AbsentObject:
    """Something a consumer asserted that the owner does not and will not have."""

    name: str
    kind: str
    reason: str


#: Every entry here corresponds to a real failing check from the 2026-09-19
#: peer startup gate.  Keeping them named is what makes the contract usable as
#: a refutation and not merely as a catalog.
NOT_PROVIDED: tuple[AbsentObject, ...] = (
    AbsentObject(
        "quant.canonical_bars_daily.adjustment_state",
        "column",
        "adjustment_state is a per-day value computed by the owner health panel "
        "(app/daily_control_plane.py); it is not stored on any table and never will be.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors.adjustment_state",
        "column",
        "Same as above: a computed control-plane state, not a column.",
    ),
    AbsentObject(
        "quant.daily_trade_limits.adjustment_state",
        "column",
        "Same as above: a computed control-plane state, not a column.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors.factor_semantics",
        "column",
        "Factor semantics are stored inside the raw JSON document. Read "
        "raw->>'factor_semantics'; see enumerations.factor_semantics for the live value set.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors.retired_at",
        "column",
        "The owner does not retire factor rows in place; a missing row means the factor is absent for that day.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors.adj_factor NULL",
        "constraint",
        "adj_factor is NOT NULL by design. The table stores measured values only and "
        "writes no placeholder row for a day without a factor.",
    ),
    AbsentObject(
        "quant.canonical_bars_daily_cold",
        "table",
        "Market-data tables are not tiered. Only the five evidence twins plus "
        "legacy_source_records live in the cold tablespace; see cold_tier.",
    ),
    AbsentObject(
        "quant.daily_fundamentals_cold",
        "table",
        "Market-data tables are not tiered; see cold_tier for what is.",
    ),
    AbsentObject(
        "quant.daily_trade_limits_cold",
        "table",
        "Market-data tables are not tiered; see cold_tier for what is.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors_cold",
        "table",
        "Market-data tables are not tiered; see cold_tier for what is.",
    ),
    AbsentObject(
        "quant.security_suspensions_cold",
        "table",
        "Market-data tables are not tiered; see cold_tier for what is.",
    ),
    AbsentObject(
        "adjustment guard indexes",
        "index",
        "The owner's adjustment guards are in-process SQL checks "
        "(identity_factor_leak_sql, factor_value_mismatch_sql), not indexes. "
        "The real index set for each supported table is published under objects[].indexes.",
    ),
    AbsentObject(
        "/api/v1/research/storage-tiers",
        "endpoint",
        "No such endpoint exists; requests return 404. Tier membership is published "
        "under cold_tier in this contract.",
    ),
    AbsentObject(
        "cumulative_tushare",
        "value",
        "Never a valid factor semantics value in the owner database. "
        "The live value set is published under enumerations.factor_semantics.",
    ),
    AbsentObject(
        "quant.daily_adjustment_factors.raw->>'factor_semantics' NOT NULL",
        "guarantee",
        "The key is absent on most rows and its absence is not an error: for a "
        "tushare-family provider it means the legacy cumulative semantics and the "
        "factor is usable. A gate that fails closed on the missing key rejects the "
        "majority of the table. Use derived_rules.adjustment_factor_usable.",
    ),
)

#: How a consumer learns that an interruption is a deployment and not a fault.
#: The announcement cannot be served over HTTP, because HTTP is exactly what a
#: release takes away; PostgreSQL is not restarted by a release, so the table is
#: readable throughout, and the ``starting`` row is written before anything is
#: stopped -- including before a tunnel reinstall.
DEPLOY_CHANNEL: dict[str, Any] = {
    "table": "quant.owner_deploy_events",
    "phases": ["starting", "completed", "failed"],
    "grouping_column": "deploy_id",
    "surfaces": {
        "http_api": "the owner HTTP surface will restart; measured 9.3 s on 2026-09-20",
        "shared_tunnel": "the shared-peer tunnel will restart, dropping database connections; "
                         "measured 5-7 s, and skipped entirely when the tunnel code is unchanged",
    },
    "how_to_use": "Poll the newest rows. A deploy_id with a 'starting' row and no 'completed' or "
                  "'failed' row is in progress: expect failures for expected_seconds, retry rather "
                  "than alert, and hold writes if surfaces.shared_tunnel is true.",
    "windows_closed_to_releases": {
        "trading_session": "09:15-15:10 Asia/Shanghai on exchange trading days",
        "peer_batch_window": "06:30-08:00 Asia/Shanghai on weekdays",
    },
    "note": "A release may still be published inside a closed window when a fix has to ship; the "
            "announcement is written either way, so the table is the truth, not the policy.",
}

#: Owner HTTP endpoints an external consumer may call, with the shared read key.
PUBLISHED_ENDPOINTS: tuple[dict[str, str], ...] = (
    {"path": "/api/v1/peer/contract", "method": "GET", "purpose": "this document"},
    {"path": "/api/v1/peer/errors", "method": "GET", "purpose": "errors the owner cluster attributed to your role"},
    {"path": "/health", "method": "GET", "purpose": "owner service health"},
)

_COLUMNS_SQL = """
SELECT c.relname,
       a.attname,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull,
       pg_get_expr(d.adbin, d.adrelid) AS column_default
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
  LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
 WHERE n.nspname = %s AND c.relname = ANY(%s)
 ORDER BY c.relname, a.attnum
"""

_INDEXES_SQL = """
SELECT c.relname, i.indexname, i.indexdef
  FROM pg_indexes i
  JOIN pg_class c ON c.relname = i.tablename
  JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = i.schemaname
 WHERE i.schemaname = %s AND i.tablename = ANY(%s)
 ORDER BY i.tablename, i.indexname
"""

_RELATION_SQL = """
SELECT c.relname,
       COALESCE(t.spcname, (SELECT spcname FROM pg_tablespace WHERE oid =
           (SELECT dattablespace FROM pg_database WHERE datname = current_database()))) AS tablespace,
       has_table_privilege(%s, c.oid, 'SELECT') AS can_select,
       has_table_privilege(%s, c.oid, 'INSERT') AS can_insert,
       has_table_privilege(%s, c.oid, 'UPDATE') AS can_update,
       has_table_privilege(%s, c.oid, 'DELETE') AS can_delete
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_tablespace t ON t.oid = c.reltablespace
 WHERE n.nspname = %s AND c.relname = ANY(%s)
"""

_COLD_SQL = """
SELECT n.nspname || '.' || c.relname
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_tablespace t ON t.oid = c.reltablespace
 WHERE t.spcname = %s AND c.relkind IN ('r', 'p')
 ORDER BY 1
"""

_SEMANTICS_SQL = """
SELECT DISTINCT raw->>'factor_semantics'
  FROM quant.daily_adjustment_factors
 WHERE raw ? 'factor_semantics'
 ORDER BY 1
"""

#: Coverage, not just the value set.  A consumer that knows only *which* values
#: are legal will assume every row carries one; these counts say out loud that
#: almost none do and that the key only starts on ``first_labelled_trading_date``.
_FACTOR_COVERAGE_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE raw ? 'factor_semantics'),
       count(*) FILTER (WHERE raw ? %s),
       min(trading_date) FILTER (WHERE raw ? 'factor_semantics'),
       max(trading_date)
  FROM quant.daily_adjustment_factors
"""

_FACTOR_PROVIDERS_SQL = """
SELECT DISTINCT provider
  FROM quant.daily_adjustment_factors
 ORDER BY 1
"""


def _as_date_text(value: Any) -> str | None:
    """Render a date/None as ISO text, so the contract stays JSON-serializable."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def _fetch(connection: Any, sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
    """Read rows as plain tuples whatever row factory the pool was built with.

    The service's own pool uses ``dict_row`` while tests and ad-hoc scripts use
    the psycopg default, and ``dict_row`` preserves the SELECT column order, so
    normalizing here keeps the queries readable as positional SQL.
    """
    cursor = connection.execute(sql, params) if params is not None else connection.execute(sql)
    return [tuple(row.values()) if isinstance(row, Mapping) else tuple(row) for row in cursor.fetchall()]


def build_contract(connection: Any, *, peer_role: str = "stock_peer") -> dict[str, Any]:
    """Introspect the live cluster and return the published contract.

    ``connection`` is any DB-API connection; every statement here is a catalog
    read or a bounded DISTINCT, so the caller may (and should) run it inside a
    read-only transaction.
    """
    names = [item.name for item in SUPPORTED_OBJECTS]

    columns: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for relname, attname, data_type, notnull, default in _fetch(connection, _COLUMNS_SQL, (SCHEMA, names)):
        columns[relname].append({
            "name": attname,
            "type": data_type,
            "nullable": not notnull,
            "default": default,
        })

    indexes: dict[str, list[dict[str, str]]] = {name: [] for name in names}
    for relname, indexname, indexdef in _fetch(connection, _INDEXES_SQL, (SCHEMA, names)):
        indexes[relname].append({"name": indexname, "definition": indexdef})

    relations: dict[str, dict[str, Any]] = {}
    for row in _fetch(connection, _RELATION_SQL, (peer_role, peer_role, peer_role, peer_role, SCHEMA, names)):
        relname, tablespace, can_select, can_insert, can_update, can_delete = row
        relations[relname] = {
            "tablespace": tablespace,
            "privileges": {
                "select": bool(can_select),
                "insert": bool(can_insert),
                "update": bool(can_update),
                "delete": bool(can_delete),
            },
        }

    objects: list[dict[str, Any]] = []
    for item in SUPPORTED_OBJECTS:
        relation = relations.get(item.name)
        objects.append({
            "name": f"{SCHEMA}.{item.name}",
            "purpose": item.purpose,
            "declared_access": item.access,
            "exists": relation is not None,
            "tablespace": (relation or {}).get("tablespace"),
            "granted_privileges": (relation or {}).get("privileges"),
            "columns": columns.get(item.name, []),
            "indexes": indexes.get(item.name, []),
        })

    cold_tables = [row[0] for row in _fetch(connection, _COLD_SQL, (COLD_TABLESPACE,))]
    semantics = [row[0] for row in _fetch(connection, _SEMANTICS_SQL) if row[0] is not None]
    coverage = _fetch(connection, _FACTOR_COVERAGE_SQL, (SUPERSEDED_MARKER,))[0]
    factor_providers = [row[0] for row in _fetch(connection, _FACTOR_PROVIDERS_SQL)]
    head_rows = _fetch(connection, f"SELECT version_num FROM {SCHEMA}.alembic_version")
    identity = _fetch(
        connection,
        "SELECT current_database(), inet_server_port(), current_setting('server_version')",
    )[0]

    return {
        "contract_version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": identity[0],
        "server_port": identity[1],
        "server_version": identity[2],
        "alembic_head": head_rows[0][0] if head_rows else None,
        "peer_role": peer_role,
        "objects": objects,
        "cold_tier": {
            "tablespace": COLD_TABLESPACE,
            "tables": cold_tables,
            "peer_readable": False,
            "note": "Operations-only. No SELECT is granted to the peer role and none will be; "
                    "tier membership is reported here so no consumer needs to query pg_class for it.",
        },
        "enumerations": {
            "factor_semantics": {
                "source": "quant.daily_adjustment_factors.raw->>'factor_semantics'",
                "values": semantics,
                "coverage": {
                    "rows_total": coverage[0],
                    "rows_with_key": coverage[1],
                    "first_labelled_trading_date": _as_date_text(coverage[3]),
                    "last_trading_date": _as_date_text(coverage[4]),
                },
                "note": "The key is a late addition and is absent on most rows, including some "
                        "rows of the most recent settled day. An absent key does NOT mean the "
                        "factor is missing and is NOT an error: on a tushare-family provider it "
                        "means the legacy cumulative semantics. Whether a row may be used is not "
                        "answered by this enumeration alone -- see "
                        "derived_rules.adjustment_factor_usable.",
            },
        },
        "derived_rules": {
            "adjustment_factor_usable": {
                "question": "May this quant.daily_adjustment_factors row back a bar adj_factor?",
                "sql_predicate": promotable_factor_evidence_sql("factor", "raw"),
                "reads": ["factor.provider", "factor.raw->>'factor_semantics'",
                          f"factor.raw->>'{SUPERSEDED_MARKER}'"],
                "promotable_provider_prefix": PROMOTABLE_FACTOR_PROVIDER_PREFIX,
                "promotable_semantics_for_prefix": list(PROMOTABLE_FACTOR_SEMANTICS),
                "derived_provider": DERIVED_FACTOR_PROVIDER,
                "derived_provider_required_semantics": CUMULATIVE_FACTOR_SEMANTICS,
                "superseded_marker": SUPERSEDED_MARKER,
                "rows_superseded": coverage[2],
                "observed_providers": factor_providers,
                "note": "The predicate is emitted from the owner's own implementation "
                        "(app/tushare_normalization.promotable_factor_evidence_sql), so it cannot "
                        "drift from what the owner actually does. Three inputs, not one: the "
                        "provider must be a tushare-family route with absent-or-cumulative "
                        "semantics, or the single derived provider with cumulative semantics "
                        "stated explicitly; and a row carrying the superseded marker is never "
                        "usable however it is labelled. Rows from any other provider are stored "
                        "as evidence of what a vendor published and must not back a price.",
            },
        },
        "not_provided": [
            {"name": item.name, "kind": item.kind, "reason": item.reason} for item in NOT_PROVIDED
        ],
        "endpoints": list(PUBLISHED_ENDPOINTS),
        "deploy_channel": DEPLOY_CHANNEL,
        "rules": [
            "Assert only against objects[], enumerations[] and derived_rules[]. Anything absent "
            "from this document is not part of the agreement even if your role can currently "
            "read it.",
            "Never fail closed on a missing JSON key. Absence is a documented state here, not a "
            "fault; enumerations[].coverage says how common it is, and a derived rule -- not a "
            "key test -- decides whether a row may be used.",
            "Never infer a schema from prose in a handoff document. If this contract and a document "
            "disagree, this contract is authoritative.",
            "A blocking startup check must have passed against this contract at least once before it "
            "is allowed to block. Run it in report-only mode first.",
            "The owner HTTP surface restarts on every release (about 10 seconds) and the shared "
            "tunnel sometimes with it (about 7 seconds). Retry with backoff and keep writes "
            "idempotent; a single failed call is not an outage. See deploy_channel.",
        ],
    }


__all__ = [
    "AbsentObject",
    "CONTRACT_VERSION",
    "DEPLOY_CHANNEL",
    "COLD_TABLESPACE",
    "NOT_PROVIDED",
    "PUBLISHED_ENDPOINTS",
    "SUPPORTED_OBJECTS",
    "SupportedObject",
    "build_contract",
]
