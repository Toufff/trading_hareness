"""Pure contracts for the one-way stock-brain SQLite migration."""

from __future__ import annotations

import base64
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


DURABLE_FACT_TABLES = frozenset({
    "board_snapshots", "documents", "entities", "entity_nodes", "event_documents",
    "event_entities", "events", "evidence", "global_market_observations",
    "intel_claims", "intel_items", "market_observations", "market_sessions",
    "market_snapshots", "personal_reviews", "position_snapshots", "predictions",
    "security_classifications", "security_order_flow_daily", "source_endpoints",
    "source_observations", "source_registry", "sources", "transactions", "zt_snapshots",
})

RESEARCH_EVIDENCE_TABLES = frozenset({
    "alpha_scores", "candidate_signals", "claim_evidence", "claims",
    "cross_market_assessment_items", "cross_market_assessment_runs",
    "cross_market_contributions", "cross_market_evaluations", "cross_market_rules",
    "entity_aliases", "entity_relations", "feature_definitions", "feature_values",
    "fund_research_runs", "investment_theses", "market_behavior_evidence",
    "market_views", "opportunity_assessments", "pool_evaluations", "pool_memberships",
    "quant_factor_definitions", "quant_factor_evaluations", "quant_factor_runs",
    "quant_walk_forward_folds", "quant_walk_forward_runs", "research_baselines",
    "research_factor_scores", "research_gate_revisions", "research_gates",
    "research_runs", "research_signal_links", "risk_assessments", "sector_views",
    "sentiment_evaluations", "sentiment_evidence_links", "sentiment_model_versions",
    "sentiment_observations", "sentiment_states", "short_term_scan_results",
    "short_term_scan_runs", "signal_evaluations", "signal_sentiment_links", "stock_calls",
    "strategy_definitions", "strategy_evaluations", "strategy_gate_decisions",
    "strategy_portfolio_snapshots", "strategy_portfolio_targets",
    "strategy_replay_evaluations", "strategy_replay_runs", "strategy_runs",
    "strategy_signal_templates", "trade_setup_events", "trade_setups",
})

ARCHIVE_ONLY_TABLES = frozenset({"reports"})

# These rows describe failed/retired orchestration, caches, queues, schema
# history or reconstructible search indexes. They are counted in receipts but
# cannot become evidence or recommendations in the new system.
EXCLUDED_TABLES = frozenset({
    "broker_fact_sync_runs", "broker_group_audits", "broker_sync_runs",
    "dataset_catalog", "dataset_retention_policies", "decision_actions",
    "decision_live_attempts", "decision_live_deliveries", "decision_node_expectations",
    "decision_replay_runs", "decision_runs", "decision_session_runs",
    "decision_validation_runs", "document_chunks", "external_sync_state",
    "human_reviews", "ingestion_runs", "object_tags", "pool_refresh_runs", "portfolios",
    "research_batch_members", "research_batches", "research_queue",
    "research_reassessments", "rule_versions", "scan_candidates", "scan_runs",
    "schema_migrations", "tags", "verification_tasks",
})

KNOWN_TABLES = DURABLE_FACT_TABLES | RESEARCH_EVIDENCE_TABLES | ARCHIVE_ONLY_TABLES | EXCLUDED_TABLES


def table_classification(table: str) -> str | None:
    if table in DURABLE_FACT_TABLES:
        return "durable_fact"
    if table in RESEARCH_EVIDENCE_TABLES:
        return "research_evidence"
    if table in ARCHIVE_ONLY_TABLES:
        return "archive_only"
    if table in EXCLUDED_TABLES:
        return None
    raise ValueError(f"unclassified stock-brain table: {table}")


def normalize_symbol(value: Any) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    match = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", raw)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        return raw
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("4", "8")):
            return f"{raw}.BJ"
        if raw.startswith(("5", "6", "9")):
            return f"{raw}.SH"
        return f"{raw}.SZ"
    # Global tickers retain their vendor symbol. They are context evidence and
    # are never fed through the A-share mainboard eligibility gate.
    return raw


def exchange_for_symbol(symbol: str, *, venue: Any = None, market: Any = None) -> str:
    if symbol.endswith(".SH"):
        return "SSE"
    if symbol.endswith(".SZ"):
        return "SZSE"
    if symbol.endswith(".BJ"):
        return "BSE"
    if venue:
        return str(venue)
    return str(market or "GLOBAL")


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        # PostgreSQL text/JSONB cannot represent NUL and rejects unpaired UTF-16
        # surrogates. Preserve such legacy bytes reversibly instead of silently
        # deleting or replacing evidence text.
        if "\x00" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            encoded = value.encode("utf-8", errors="surrogatepass")
            return {
                "encoding": "utf-8-base64",
                "data": base64.b64encode(encoded).decode("ascii"),
                "reason": "postgresql-text-incompatible",
            }
        return value
    if isinstance(value, (datetime, date, Decimal)):
        return str(value)
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item) for item in value]
    return str(value)


def parse_json_value(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def normalized_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {str(key): json_safe(value) for key, value in row.items() if key != "__rowid__"}
    return payload


def payload_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_row_key(row: Mapping[str, Any], primary_key_columns: Sequence[str]) -> str:
    if primary_key_columns:
        key = [json_safe(row.get(column)) for column in primary_key_columns]
        return json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(row["__rowid__"])


def evidence_timestamps(row: Mapping[str, Any]) -> tuple[Any, Any]:
    effective = next((row.get(key) for key in (
        "event_time", "occurred_at", "published_at", "as_of", "data_as_of",
        "observed_at", "trade_date", "local_trade_date", "date",
    ) if row.get(key)), None)
    available = next((row.get(key) for key in (
        "available_at", "received_at", "fetched_at", "discovered_at", "created_at",
    ) if row.get(key)), None)
    return coerce_timestamp(effective), coerce_timestamp(available)


def coerce_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed


__all__ = [
    "KNOWN_TABLES", "coerce_timestamp", "evidence_timestamps", "exchange_for_symbol", "json_safe",
    "normalize_symbol", "normalized_payload", "parse_json_value", "payload_sha256",
    "source_row_key", "table_classification",
]
