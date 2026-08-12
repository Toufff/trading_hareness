from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Json

from .analysis import EXTRACTOR_VERSION, direction_source, extract_signals
from .database import Database


REMOTE_EXTRACTOR_VERSION = "remote-report-normalizer-v2"
REMOTE_TOPIC_EXTRACTOR_VERSION = "remote-report-normalizer-v3"

TOPIC_TERMS = (
    "半导体材料", "电子特气", "先进封装", "电子布", "MLCC", "PCB", "有色铜", "铜加工",
    "金属钨", "AI应用", "人工智能应用", "硬件科技", "有色金属",
    "黄金", "金矿", "反制概念",
)

REMOTE_TEXT_FIELDS = (
    "analyst", "analyst_id", "report_id", "date", "title", "summary", "version", "content_hash",
    "created_at", "updated_at", "mentioned_stocks", "mentioned_sectors", "predictions",
)


def strip_media_references(value: str) -> str:
    """Keep extracted prose while removing remote media and transcript links.

    Remote reports can contain Markdown that points at a screenshot, video or
    a source transcript.  The archive is intentionally text-only: it neither
    fetches those links nor retains them as evidence locations.  Anchor text is
    kept only when it is meaningful prose; boilerplate "完整原文" links vanish.
    """
    text = re.sub(r"!\[[^\]]*\]\([^\)]*\)", "", value)
    text = re.sub(r"\[完整原文\]\([^\)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return "\n".join(line.rstrip() for line in text.splitlines() if line.strip()).strip()


def text_only_value(value: Any) -> Any:
    if isinstance(value, str):
        return strip_media_references(value)
    if isinstance(value, list):
        return [text_only_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): text_only_value(item) for key, item in value.items()}
    return value


def text_only_remote_report(report: dict[str, Any]) -> dict[str, Any]:
    """Select only the archive's already-extracted textual report fields.

    This is an ingress guardrail, not merely a UI convention.  It drops
    `materials` and `source_url` before persistence, so an n8n sync cannot turn
    a report ingest into a remote image/audio/video fetch or a media archive.
    """
    normalized = {key: text_only_value(report.get(key)) for key in REMOTE_TEXT_FIELDS if key in report}
    normalized["raw_markdown"] = strip_media_references(str(report.get("raw_markdown") or ""))
    normalized["sections"] = text_only_value(report.get("sections") or {})
    normalized["materials"] = []
    normalized["source_url"] = None
    return normalized


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def horizon_days(text: str) -> int:
    lowered = text.lower()
    if any(token in lowered for token in ("明日", "次日", "明天", "next day")):
        return 1
    if any(token in lowered for token in ("短线", "本周", "一周", "5日")):
        return 5
    if any(token in lowered for token in ("中线", "一个月", "20日")):
        return 20
    if any(token in lowered for token in ("长线", "长期", "60日")):
        return 60
    return 20


def classify_remote_text(text: str) -> tuple[int, float, float]:
    positive = sum(token in text for token in ("看多", "看好", "买入", "加仓", "布局", "增持", "机会"))
    negative = sum(token in text for token in ("看空", "回避", "减仓", "卖出", "风险", "止损", "谨慎"))
    if positive > negative:
        return 1, min(0.95, 0.55 + positive * 0.1), min(0.9, 0.55 + positive * 0.08)
    if negative > positive:
        return -1, min(0.95, 0.55 + negative * 0.1), min(0.9, 0.55 + negative * 0.08)
    return 0, 0.5, 0.4


def evidence_fragments(report: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    report = text_only_remote_report(report)
    fragments: list[tuple[str, str, dict[str, Any]]] = []
    markdown = str(report.get("raw_markdown") or "")
    if markdown:
        fragments.append(("raw_markdown", markdown, {"source_url": report.get("source_url")}))
    summary = str(report.get("summary") or "")
    if summary and summary != markdown:
        fragments.append(("summary", summary, {}))
    sections = report.get("sections")
    if isinstance(sections, dict):
        for key, value in sections.items():
            body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if body.strip():
                fragments.append((f"section:{key}", body, {"section": key}))
    return fragments


def labels(values: Any) -> list[str]:
    """Accept the archive's string or object labels without losing provenance."""
    result: list[str] = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("name") or value.get("label") or value.get("symbol") or value.get("title")
        label = str(value or "").strip()
        if label and label not in result:
            result.append(label)
    return result


def normalize_topic_key(label: str) -> str:
    compact = re.sub(r"\s+", "", label.strip().lower())
    return f"remote:{compact}"


def extract_topics(text: str) -> list[str]:
    """Extract deterministic theme labels without inventing stock mappings."""
    normalized = text.replace("ＡＩ", "AI").replace("ai", "AI")
    result: list[str] = []
    for term in TOPIC_TERMS:
        if term in normalized and term not in result:
            result.append(term)
    return result


def report_topic_labels(report: dict[str, Any], body: str) -> list[str]:
    values = [
        *labels(report.get("mentioned_sectors")),
        *labels(report.get("predictions")),
        *extract_topics(str(report.get("summary") or "")),
        *extract_topics(body),
    ]
    result: list[str] = []
    for value in values:
        label = str(value).strip()
        if label and label not in result:
            result.append(label)
    return result


def revision_type(connection: Any, analyst_id: str, scope: str, subject_key: str, direction: int, evidence_id: Any) -> tuple[str, Any | None]:
    prior = connection.execute(
        """SELECT claim_id,direction,strength FROM quant.analyst_claims
           WHERE remote_analyst_id=%s AND scope=%s AND subject_key=%s AND evidence_id<>%s
           ORDER BY available_at DESC,created_at DESC LIMIT 1""",
        (analyst_id, scope, subject_key, evidence_id),
    ).fetchone()
    if prior is None:
        return "new", None
    if int(prior["direction"]) != direction and direction != 0:
        return "reverse", prior["claim_id"]
    return "confirm", prior["claim_id"]


def resolve_instrument_symbol(connection: Any, label: str) -> str | None:
    """Resolve only an exact known listing name or an explicit Tushare code.

    Remote reports frequently use abbreviated company names.  Guessing a code
    from fuzzy text would turn editorial prose into a potentially unsafe trade
    signal, so unresolved labels are sent to the review queue instead.
    """
    explicit = re.search(r"\b(\d{6}\.(?:SH|SZ|BJ))\b", label.upper())
    if explicit:
        return explicit.group(1)
    row = connection.execute(
        "SELECT symbol FROM quant.instruments WHERE lower(coalesce(name,''))=lower(%s) LIMIT 1",
        (label,),
    ).fetchone()
    return str(row["symbol"]) if row else None


def persist_claim_revision(connection: Any, analyst_id: str, scope: str, subject_key: str, direction: int,
                           evidence_id: Any, claim_id: Any) -> None:
    kind, prior_claim_id = revision_type(connection, analyst_id, scope, subject_key, direction, evidence_id)
    connection.execute(
        "INSERT INTO quant.claim_revisions(prior_claim_id,claim_id,revision_type) VALUES(%s,%s,%s) ON CONFLICT(claim_id,revision_type) DO NOTHING",
        (prior_claim_id, claim_id, kind),
    )


def import_remote_report(db: Database, report: dict[str, Any], force_reprocess: bool = False) -> dict[str, Any]:
    report = text_only_remote_report(report)
    analyst = report.get("analyst") or {}
    analyst_id = str(analyst.get("analyst_id") or report.get("analyst_id") or "").strip()
    report_id = str(report.get("report_id") or "").strip()
    report_date = str(report.get("date") or "").strip()
    version = str(report.get("version") or "").strip()
    content_hash = str(report.get("content_hash") or "").strip()
    if not analyst_id or not report_id or not report_date or not version or not content_hash:
        raise ValueError("remote report requires analyst_id, report_id, date, version and content_hash")
    available_at = parse_timestamp(report.get("updated_at") or report.get("created_at"))
    markdown = str(report.get("raw_markdown") or "")
    evidence_count = 0
    claims_count = 0
    with db.transaction() as connection:
        connection.execute(
            """INSERT INTO quant.remote_analysts(remote_analyst_id,name,organization,description,remote_updated_at,remote_metadata,synced_at)
               VALUES(%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT(remote_analyst_id) DO UPDATE SET name=EXCLUDED.name,organization=EXCLUDED.organization,
               description=EXCLUDED.description,remote_updated_at=EXCLUDED.remote_updated_at,remote_metadata=EXCLUDED.remote_metadata,synced_at=now()""",
            (analyst_id, str(analyst.get("name") or analyst_id), str(analyst.get("organization") or ""),
             str(analyst.get("description") or ""), available_at, Json(analyst)),
        )
        previous = connection.execute(
            "SELECT remote_version,content_hash,raw_markdown,sections,materials FROM quant.remote_reports WHERE remote_report_id=%s", (report_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,summary,source_url,remote_version,content_hash,
                 remote_created_at,remote_updated_at,raw_markdown,sections,materials,mentioned_stocks,mentioned_sectors,predictions,payload,synced_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT(remote_report_id) DO UPDATE SET remote_analyst_id=EXCLUDED.remote_analyst_id,report_date=EXCLUDED.report_date,
                 title=EXCLUDED.title,summary=EXCLUDED.summary,source_url=EXCLUDED.source_url,remote_version=EXCLUDED.remote_version,
                 content_hash=EXCLUDED.content_hash,remote_updated_at=EXCLUDED.remote_updated_at,raw_markdown=EXCLUDED.raw_markdown,
                 sections=EXCLUDED.sections,materials=EXCLUDED.materials,mentioned_stocks=EXCLUDED.mentioned_stocks,
                 mentioned_sectors=EXCLUDED.mentioned_sectors,predictions=EXCLUDED.predictions,payload=EXCLUDED.payload,synced_at=now()""",
            (report_id, analyst_id, report_date, str(report.get("title") or ""), str(report.get("summary") or ""), report.get("source_url"),
             version, content_hash, parse_timestamp(report.get("created_at")), available_at, markdown, Json(report.get("sections") or {}),
             Json(report.get("materials") or []), Json(report.get("mentioned_stocks") or []), Json(report.get("mentioned_sectors") or []),
             Json(report.get("predictions") or []), Json(report)),
        )
        connection.execute(
            """INSERT INTO quant.remote_report_versions(remote_report_id,remote_version,content_hash,payload)
               VALUES(%s,%s,%s,%s)
               ON CONFLICT(remote_report_id,remote_version,content_hash) DO UPDATE SET last_seen_at=now(),payload=EXCLUDED.payload""",
            (report_id, version, content_hash, Json(report)),
        )
        sanitized_content_changed = previous is not None and (
            str(previous["raw_markdown"] or "") != markdown
            or dict(previous["sections"] or {}) != dict(report.get("sections") or {})
            or list(previous["materials"] or []) != []
        )
        changed = (previous is None or previous["remote_version"] != version or previous["content_hash"] != content_hash
                   or sanitized_content_changed)
        if not changed and not force_reprocess:
            return {"status": "unchanged", "remote_report_id": report_id, "evidence": 0, "claims": 0}
        if force_reprocess or sanitized_content_changed:
            connection.execute(
                """DELETE FROM quant.analyst_claims c
                   USING quant.analyst_evidence e
                   WHERE c.evidence_id=e.evidence_id AND e.remote_report_id=%s""",
                (report_id,),
            )
            connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (report_id,))
        for evidence_key, body, location in evidence_fragments(report):
            row = connection.execute(
                """INSERT INTO quant.analyst_evidence(remote_report_id,evidence_key,evidence_type,body,location,content_sha256,available_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(remote_report_id,evidence_key,content_sha256) DO UPDATE SET available_at=EXCLUDED.available_at
                   RETURNING evidence_id""",
                (report_id, evidence_key, evidence_key.split(":", 1)[0], body, Json(location), text_hash(body), available_at),
            ).fetchone()
            evidence_count += 1
            direction, strength, confidence = classify_remote_text(body)
            for signal in extract_signals(body):
                connection.execute(
                    "INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'remote-report') ON CONFLICT(symbol) DO NOTHING",
                    (signal.symbol, signal.exchange),
                )
                claim = connection.execute(
                    """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,horizon_days,
                         extraction_confidence,extractor_version,available_at,raw)
                       VALUES(%s,%s,'stock',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                         strength=EXCLUDED.strength,extraction_confidence=EXCLUDED.extraction_confidence,raw=EXCLUDED.raw
                       RETURNING claim_id""",
                    (row["evidence_id"], analyst_id, signal.symbol, signal.symbol, signal.direction, signal.strength, signal.horizon_days,
                     signal.extraction_confidence, REMOTE_EXTRACTOR_VERSION, available_at, Json({
                         "remote_report_id": report_id, "evidence_key": evidence_key,
                         "direction_source": direction_source(signal.evidence_text),
                         "evidence_text": signal.evidence_text,
                     })),
                ).fetchone()
                persist_claim_revision(connection, analyst_id, "stock", signal.symbol, signal.direction, row["evidence_id"], claim["claim_id"])
                claims_count += 1
            topic_values = report_topic_labels(report, body)
            for scope, values in (("stock", labels(report.get("mentioned_stocks"))), ("theme", topic_values)):
                for label in values:
                    if scope == "stock":
                        symbol = resolve_instrument_symbol(connection, label)
                        if symbol is None:
                            connection.execute(
                                """INSERT INTO quant.claim_review_queue(evidence_id,suggested_scope,suggested_label,direction,strength,horizon_days,
                                     extraction_confidence,raw)
                                   VALUES(%s,'stock',%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(evidence_id,suggested_scope,suggested_label) DO NOTHING""",
                                (row["evidence_id"], label, direction, strength, horizon_days(body), confidence,
                                 Json({"remote_report_id": report_id, "evidence_key": evidence_key, "source_label": label})),
                            )
                            continue
                        subject_key = symbol
                    else:
                        subject_key = normalize_topic_key(label)
                    claim = connection.execute(
                        """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,horizon_days,
                         extraction_confidence,extractor_version,available_at,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                         strength=EXCLUDED.strength,raw=EXCLUDED.raw RETURNING claim_id""",
                        (row["evidence_id"], analyst_id, scope, subject_key, label, direction, strength, horizon_days(body), confidence,
                         REMOTE_TOPIC_EXTRACTOR_VERSION if scope == "theme" else REMOTE_EXTRACTOR_VERSION, available_at,
                         Json({"remote_report_id": report_id, "evidence_key": evidence_key, "source_label": label})),
                    ).fetchone()
                    persist_claim_revision(connection, analyst_id, scope, subject_key, direction, row["evidence_id"], claim["claim_id"])
                    claims_count += 1
    return {"status": "updated", "remote_report_id": report_id, "evidence": evidence_count, "claims": claims_count}


def reprocess_remote_reports(db: Database, limit: int = 100) -> dict[str, Any]:
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT payload FROM quant.remote_reports ORDER BY synced_at DESC LIMIT %s",
            (max(1, min(limit, 500)),),
        ).fetchall()
    results = [import_remote_report(db, dict(row["payload"]), force_reprocess=True) for row in rows]
    return {
        "status": "completed",
        "reports": len(results),
        "evidence": sum(int(item.get("evidence", 0)) for item in results),
        "claims": sum(int(item.get("claims", 0)) for item in results),
        "items": results,
    }


def remote_report_list_state(db: Database) -> dict[str, Any]:
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT remote_analyst_id,count(*)::int reports,max(report_date) latest_report_date,max(synced_at) last_synced_at FROM quant.remote_reports GROUP BY remote_analyst_id ORDER BY remote_analyst_id"
        ).fetchall()
    return {"analysts": rows}
