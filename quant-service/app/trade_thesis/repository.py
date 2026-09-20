"""Synchronous, transactional persistence for immutable trade-thesis facts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from psycopg.types.json import Json

from .contracts import immutable_thesis


class ThesisConflict(RuntimeError):
    """The caller based a proposal/review on stale or mismatched state."""


class ThesisNotFound(LookupError):
    pass


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         default=str, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _one(connection: Any, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row
    raise TypeError("trade thesis repository requires dict_row connections")


def persist_revision_event(
    connection: Any, *, thesis_id: str, symbol: str, event_type: str,
    content_revision: int, base_revision: int | None, payload: dict[str, Any],
    actor: str, proposal_hash: str | None = None, review_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a proposal/capture/review event; never update an earlier event."""
    digest = stable_hash(payload)
    existing = _one(connection, """
        SELECT event_id,event_type,content_revision,content_hash,created_at
          FROM quant.trade_thesis_revisions
         WHERE thesis_id=%s AND event_type=%s AND content_hash=%s
    """, (thesis_id, event_type, digest))
    if existing:
        return {"status": "idempotent", **dict(existing)}
    try:
        row = _one(connection, """
            INSERT INTO quant.trade_thesis_revisions(
                thesis_id,symbol,event_type,content_revision,base_revision,payload,
                actor,proposal_hash,review_data,content_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING event_id,event_type,content_revision,content_hash,created_at
        """, (thesis_id, symbol, event_type, content_revision, base_revision,
              Json(payload), actor, proposal_hash, Json(review_data or {}), digest))
    except Exception as error:
        if getattr(error, "sqlstate", None) == "23505":
            raise ThesisConflict("expected_revision is stale") from error
        raise
    return {"status": "created", **dict(row or {})}


def capture(connection: Any, thesis: dict[str, Any], *, actor: str = "scan_adapter") -> dict[str, Any]:
    """Freeze a newly observed thesis, returning the original fact on a retry."""
    frozen = _one(connection, """
        SELECT event_id,content_hash,created_at,payload FROM quant.trade_thesis_revisions
         WHERE thesis_id=%s AND event_type='capture' AND content_revision=%s
         ORDER BY event_seq LIMIT 1 FOR SHARE
    """, (str(thesis["thesis_id"]), int(thesis["revision"])))
    if frozen is not None:
        if stable_hash(thesis) != str(frozen["content_hash"]):
            raise ThesisConflict("captured thesis revision payload is immutable")
        return {"status": "frozen", **dict(frozen), "thesis": dict(frozen["payload"])}
    result = persist_revision_event(
        connection, thesis_id=str(thesis["thesis_id"]), symbol=str(thesis["symbol"]),
        event_type="capture", content_revision=int(thesis["revision"]),
        base_revision=thesis.get("parent_revision"), payload=thesis, actor=actor,
    )
    stored = _one(connection, """
        SELECT payload FROM quant.trade_thesis_revisions
         WHERE thesis_id=%s AND event_type='capture' AND content_revision=%s
         ORDER BY event_seq LIMIT 1
    """, (str(thesis["thesis_id"]), int(thesis["revision"])))
    return {**result, "thesis": None if stored is None else dict(stored["payload"])}


def latest_previous_evaluation(connection: Any, thesis_id: str, cutoff_at: str,
                               namespace: str = "shadow") -> dict[str, Any] | None:
    row = _one(connection, """
        SELECT result FROM quant.trade_thesis_evaluations
         WHERE thesis_id=%s AND namespace=%s AND cutoff_at<%s
         ORDER BY cutoff_at DESC,created_at DESC LIMIT 1
    """, (thesis_id, namespace, cutoff_at))
    return None if row is None else dict(row["result"])


def active_thesis(connection: Any, thesis_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
    row = _one(connection, """
        SELECT payload FROM quant.trade_thesis_revisions r
         WHERE r.thesis_id=%s AND r.event_type IN ('capture','approve')
           AND (%s::timestamptz IS NULL OR r.created_at<=%s)
           AND (event_type='capture' OR NOT EXISTS (
               SELECT 1 FROM quant.trade_thesis_revisions conflict
                WHERE conflict.thesis_id=r.thesis_id AND conflict.content_revision=r.content_revision
                  AND conflict.proposal_hash=r.proposal_hash
                  AND conflict.event_type IN ('reject','needs_evidence')))
         ORDER BY content_revision DESC,event_seq DESC LIMIT 1
    """, (thesis_id, as_of, as_of))
    return None if row is None else dict(row["payload"])


def list_latest(connection: Any, symbol: str | None = None, limit: int = 500,
                namespace: str = "shadow") -> list[dict[str, Any]]:
    rows = connection.execute("""
        WITH active AS (
          SELECT DISTINCT ON (thesis_id) thesis_id,symbol,content_revision,payload,created_at
            FROM quant.trade_thesis_revisions
           WHERE event_type IN ('capture','approve') AND (%s::text IS NULL OR symbol=%s)
             AND (event_type='capture' OR NOT EXISTS (
                 SELECT 1 FROM quant.trade_thesis_revisions conflict
                  WHERE conflict.thesis_id=trade_thesis_revisions.thesis_id
                    AND conflict.content_revision=trade_thesis_revisions.content_revision
                    AND conflict.proposal_hash=trade_thesis_revisions.proposal_hash
                    AND conflict.event_type IN ('reject','needs_evidence')))
           ORDER BY thesis_id,content_revision DESC,event_seq DESC
        ), evaluations AS (
          SELECT DISTINCT ON (thesis_id) thesis_id,result,created_at
            FROM quant.trade_thesis_evaluations WHERE namespace=%s
           ORDER BY thesis_id,cutoff_at DESC,created_at DESC
        )
        SELECT active.payload AS thesis,evaluations.result AS evaluation
          FROM active LEFT JOIN evaluations USING(thesis_id)
         ORDER BY active.created_at DESC LIMIT %s
    """, (symbol, symbol, namespace, max(1, min(limit, 500)))).fetchall()
    return [dict(row) for row in rows]


def propose_change(connection: Any, thesis_id: str, *, expected_revision: int,
                   changes: dict[str, Any], actor: str) -> dict[str, Any]:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (thesis_id,))
    active = _one(connection, """
        SELECT content_revision,payload FROM quant.trade_thesis_revisions r
         WHERE thesis_id=%s AND event_type IN ('capture','approve')
           AND (event_type='capture' OR NOT EXISTS (
               SELECT 1 FROM quant.trade_thesis_revisions conflict
                WHERE conflict.thesis_id=r.thesis_id AND conflict.content_revision=r.content_revision
                  AND conflict.proposal_hash=r.proposal_hash
                  AND conflict.event_type IN ('reject','needs_evidence')))
         ORDER BY content_revision DESC,event_seq DESC LIMIT 1 FOR SHARE
    """, (thesis_id,))
    if active is None:
        raise ThesisNotFound(thesis_id)
    current = int(active["content_revision"])
    if current != expected_revision:
        raise ThesisConflict(f"expected_revision {expected_revision} does not match active revision {current}")
    pending = _one(connection, """
        SELECT 1 AS present FROM quant.trade_thesis_revisions proposal
         WHERE proposal.thesis_id=%s AND proposal.event_type='proposal' AND proposal.base_revision=%s
           AND NOT EXISTS (
               SELECT 1 FROM quant.trade_thesis_revisions review
                WHERE review.thesis_id=proposal.thesis_id
                  AND review.content_revision=proposal.content_revision
                  AND review.proposal_hash=proposal.content_hash
                  AND review.event_type IN ('approve','reject','needs_evidence'))
         LIMIT 1
    """, (thesis_id, current))
    if pending:
        raise ThesisConflict("a proposal for expected_revision is already pending review")
    protected = {"thesis_id", "revision", "parent_revision", "symbol", "source_run_id",
                 "available_at", "effective_from", "terminal_deadline", "origin_mode", "created_at"}
    forbidden = sorted(key for key in changes if key in protected or key.startswith("original_")
                       or key in {"primary_origin_id", "related_thesis_ids", "origin_ids",
                                  "origin_primary_lane", "selector_version"})
    if forbidden:
        raise ThesisConflict("protected thesis fields cannot be changed: " + ",".join(forbidden))
    latest = _one(connection, """
        SELECT coalesce(max(content_revision),0) AS revision
          FROM quant.trade_thesis_revisions WHERE thesis_id=%s
    """, (thesis_id,))
    proposed_revision = max(current, int((latest or {}).get("revision", 0))) + 1
    proposal = {**dict(active["payload"]), **changes, "thesis_id": thesis_id,
                "revision": proposed_revision, "parent_revision": current}
    immutable_thesis(proposal)
    return persist_revision_event(
        connection, thesis_id=thesis_id, symbol=str(proposal["symbol"]), event_type="proposal",
        content_revision=proposed_revision, base_revision=current, payload=proposal, actor=actor,
    )


def review_change(connection: Any, thesis_id: str, *, revision: int, proposal_hash: str,
                  verdict: str, reviewer: str, reason: str, evidence_hash: str | None) -> dict[str, Any]:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (thesis_id,))
    proposal = _one(connection, """
        SELECT symbol,base_revision,payload,content_hash,actor FROM quant.trade_thesis_revisions
         WHERE thesis_id=%s AND event_type='proposal' AND content_revision=%s
         ORDER BY event_seq DESC LIMIT 1 FOR SHARE
    """, (thesis_id, revision))
    if proposal is None:
        raise ThesisNotFound(f"proposal {thesis_id}:{revision}")
    if proposal["content_hash"] != proposal_hash:
        raise ThesisConflict("review proposal_hash does not match the immutable proposal")
    if str(proposal["actor"]).strip().casefold() == reviewer.strip().casefold():
        raise ThesisConflict("proposal author cannot review the same proposal")
    active = active_thesis(connection, thesis_id)
    if active is None or int(active["revision"]) != int(proposal["base_revision"]):
        raise ThesisConflict("proposal base revision is no longer active")
    reviewed = _one(connection, """
        SELECT 1 AS present FROM quant.trade_thesis_revisions
         WHERE thesis_id=%s AND content_revision=%s AND proposal_hash=%s
           AND event_type IN ('approve','reject','needs_evidence') LIMIT 1
    """, (thesis_id, revision, proposal_hash))
    if reviewed:
        raise ThesisConflict("proposal already has a terminal review")
    event_payload = {
        "verdict": verdict, "reviewer": reviewer, "reason": reason,
        "proposal_revision": revision, "proposal_hash": proposal_hash,
        "evidence_hash": evidence_hash,
    }
    event_type = "approve" if verdict == "approve" else verdict
    payload = dict(proposal["payload"]) if verdict == "approve" else event_payload
    return persist_revision_event(
        connection, thesis_id=thesis_id, symbol=str(proposal["symbol"]), event_type=event_type,
        content_revision=revision, base_revision=int(proposal["base_revision"]), payload=payload,
        actor=reviewer, proposal_hash=proposal_hash, review_data=event_payload,
    )


def persist_evaluation(connection: Any, evaluation: dict[str, Any], *, namespace: str = "shadow") -> dict[str, Any]:
    payload = dict(evaluation)
    payload["evaluation_id"] = stable_hash({"evaluation_id": payload["evaluation_id"], "namespace": namespace})
    unhashed = dict(payload)
    unhashed.pop("content_hash", None)
    digest = stable_hash(unhashed)
    payload["content_hash"] = digest
    row = _one(connection, """
        INSERT INTO quant.trade_thesis_evaluations(
            evaluation_id,thesis_id,thesis_revision,source_run_id,cutoff_at,namespace,
            thesis_state,evidence_status,entry_state,input_hash,result,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (thesis_id,source_run_id,cutoff_at,namespace,content_hash)
        DO NOTHING
        RETURNING evaluation_id,created_at
    """, (payload["evaluation_id"], payload["thesis_id"], payload["thesis_revision"],
          payload["source_run_id"], payload["cutoff_at"], namespace,
          payload["states"]["thesis_state"], payload["states"]["evidence_status"],
          payload["states"]["entry_state"], payload["input_hash"], Json(payload), digest))
    if row:
        return {"status": "created", "content_hash": digest, **dict(row), "evaluation": payload}
    existing = _one(connection, """
        SELECT evaluation_id,created_at FROM quant.trade_thesis_evaluations
         WHERE thesis_id=%s AND source_run_id=%s AND cutoff_at=%s AND namespace=%s AND content_hash=%s
    """, (payload["thesis_id"], payload["source_run_id"], payload["cutoff_at"], namespace, digest))
    return {"status": "idempotent", "content_hash": digest, **dict(existing or {}), "evaluation": payload}


def persist_binding(connection: Any, binding: dict[str, Any]) -> dict[str, Any]:
    payload = dict(binding)
    digest = stable_hash(payload)
    row = _one(connection, """
        INSERT INTO quant.plan_thesis_bindings(
            account_key,symbol,position_episode_id,thesis_id,thesis_revision,plan_id,
            binding_source,bound_at,evidence_refs,content_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (content_hash) DO NOTHING RETURNING binding_id,created_at
    """, (payload["account_key"], payload["symbol"], payload.get("position_episode_id"),
          payload["thesis_id"], payload["revision"], payload.get("plan_id"),
          payload["binding_source"], payload["bound_at"], Json(payload.get("evidence_refs", [])), digest))
    if row:
        return {"status": "created", "content_hash": digest, **dict(row)}
    existing = _one(connection, "SELECT binding_id,created_at FROM quant.plan_thesis_bindings WHERE content_hash=%s", (digest,))
    return {"status": "idempotent", "content_hash": digest, **dict(existing or {})}


__all__ = ["ThesisConflict", "ThesisNotFound", "active_thesis", "capture", "latest_previous_evaluation", "list_latest", "persist_binding", "persist_evaluation",
           "persist_revision_event", "propose_change", "review_change", "stable_hash"]
