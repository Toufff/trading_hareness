from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

from psycopg.types.json import Json

from .analysis import EXTRACTOR_VERSION, direction_source, extract_signals
from .analyst_trade_actions import sync_anqiang_message_trade_actions, sync_anqiang_trade_actions
from .analyst_skill_models import rebuild_all_analyst_skill_profiles, rebuild_analyst_skill_profile
from .analyst_expert_research import rebuild_analyst_research
from .analyst_observations import persist_extraction_run, persist_observations_for_evidence
from .database import Database


REMOTE_EXTRACTOR_VERSION = "remote-report-normalizer-v2"
REMOTE_TOPIC_EXTRACTOR_VERSION = "remote-report-normalizer-v3"

TOPIC_TERMS = (
    "半导体材料", "电子特气", "先进封装", "电子布", "MLCC", "PCB", "有色铜", "铜加工",
    "金属钨", "AI应用", "人工智能应用", "硬件科技", "有色金属",
    "黄金", "金矿", "反制概念",
)

_MARKET_SCOPE_TERMS = ("大盘", "市场", "指数", "上证", "沪指", "深成", "创业板", "行情")
_EXPLICIT_ACTION_TERMS = ("买入", "卖出", "加仓", "减仓", "开仓", "止损", "回避", "看多", "看空", "看好")

REMOTE_TEXT_FIELDS = (
    "analyst", "analyst_id", "report_id", "date", "title", "summary", "version", "content_hash",
    "created_at", "updated_at", "mentioned_stocks", "mentioned_sectors", "predictions",
)

REMOTE_MESSAGE_TEXT_FIELDS = (
    "message_id", "analyst_id", "source_item_id", "source_message_id", "source_entry_id", "received_at",
    "strategy_available_at", "published_at", "edited_at", "stated_at", "stated_precision", "time_evidence",
    "type", "content", "source_ref", "version", "content_hash",
)

# Some source adapters preserve the original bot timestamp in the first text
# line but, on older archive versions, did not emit it as ``stated_at``.  The
# timestamp is only accepted when it includes a calendar date; a bare "10:30"
# in prose is not sufficiently unambiguous to turn into a replay timestamp.
_MESSAGE_BODY_TIMESTAMP = re.compile(
    r"(?m)^\s*(?:(?P<year>20\d{2})[-/.])?(?P<month>\d{1,2})-(?P<day>\d{1,2})\s+"
    r"(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?::(?P<second>[0-5]\d))?\b"
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


def text_only_remote_message(message: dict[str, Any]) -> dict[str, Any]:
    """Persist only a remote message's already-extracted text and timing proof.

    The remote API may describe an OCR/audio/video source, but this boundary
    never follows its media reference.  ``content`` is the remote service's
    extracted prose, and is the sole material available to this service.
    """
    normalized = {key: text_only_value(message.get(key)) for key in REMOTE_MESSAGE_TEXT_FIELDS if key in message}
    normalized["content"] = strip_media_references(str(message.get("content") or ""))
    normalized["time_evidence"] = text_only_value(message.get("time_evidence") or {})
    normalized["source_ref"] = str(message.get("source_ref") or "")
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


def parse_optional_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def body_stated_timestamp(value: str, *, received_at: datetime) -> tuple[datetime, str, dict[str, Any]] | None:
    """Recover a source-preserved message timestamp for author-time replay.

    This is deliberately *not* a strategy-availability fallback.  The caller
    keeps ``received_at`` immutable for live policy, while this value feeds the
    separately labelled author-timestamp evaluation ledger.  Remote structured
    ``stated_at`` still takes precedence whenever supplied.
    """
    match = _MESSAGE_BODY_TIMESTAMP.search(value)
    if match is None:
        return None
    local_received = received_at.astimezone(ZoneInfo("Asia/Shanghai"))
    try:
        stated = datetime(
            int(match.group("year") or local_received.year), int(match.group("month")), int(match.group("day")),
            int(match.group("hour")), int(match.group("minute")), int(match.group("second") or 0),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).astimezone(timezone.utc)
    except ValueError:
        return None
    raw = match.group(0).strip()
    precision = "second" if match.group("second") is not None else "minute"
    return stated, precision, {
        "source": "remote_content_timestamp",
        "time_text": raw,
        "parser": "remote-message-body-time-v1",
        "usage": "author_time_replay_only",
    }


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


def explicitness(text: str, *, scope: str) -> float:
    """Score explicit opinions without turning generic prose into a signal."""
    cue_count = sum(term in text for term in _EXPLICIT_ACTION_TERMS)
    conditional = sum(term in text for term in ("如果", "若", "只有", "不破", "跌破", "突破"))
    scoped = any(term in text for term in _MARKET_SCOPE_TERMS) if scope == "market" else True
    if not scoped:
        return 0.0
    return min(1.0, round(0.25 + cue_count * 0.16 + conditional * 0.06, 4))


def is_market_opinion(text: str) -> bool:
    direction, _, _ = classify_remote_text(text)
    return direction != 0 and any(term in text for term in _MARKET_SCOPE_TERMS)


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


def _insert_message_claim(connection: Any, *, evidence_id: Any, analyst_id: str, message_id: str, scope: str,
                          subject_key: str, subject_label: str, direction: int, strength: float, horizon: int,
                          confidence: float, extractor_version: str, body: str, published_at: datetime | None,
                          available_at: datetime) -> None:
    claim = connection.execute(
        """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,horizon_days,
             extraction_confidence,extractor_version,published_at,available_at,explicitness,raw)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
             strength=EXCLUDED.strength,extraction_confidence=EXCLUDED.extraction_confidence,published_at=EXCLUDED.published_at,
             available_at=EXCLUDED.available_at,explicitness=EXCLUDED.explicitness,raw=EXCLUDED.raw RETURNING claim_id""",
        (evidence_id, analyst_id, scope, subject_key, subject_label, direction, strength, horizon, confidence,
         extractor_version, published_at, available_at, explicitness(body, scope=scope),
         Json({"remote_message_id": message_id, "direction_source": direction_source(body), "evidence_text": body})),
    ).fetchone()
    persist_claim_revision(connection, analyst_id, scope, subject_key, direction, evidence_id, claim["claim_id"])


def _materialize_message_claims(connection: Any, *, evidence_id: Any, analyst_id: str, message_id: str,
                                body: str, published_at: datetime | None, available_at: datetime) -> int:
    """Only explicit codes and reviewed topic terms may become message claims."""
    direction, strength, confidence = classify_remote_text(body)
    count = 0
    for signal in extract_signals(body):
        connection.execute("INSERT INTO quant.instruments(symbol,exchange,source) VALUES(%s,%s,'remote-message') ON CONFLICT(symbol) DO NOTHING",
                           (signal.symbol, signal.exchange))
        _insert_message_claim(connection, evidence_id=evidence_id, analyst_id=analyst_id, message_id=message_id,
                              scope="stock", subject_key=signal.symbol, subject_label=signal.symbol, direction=signal.direction,
                              strength=signal.strength, horizon=signal.horizon_days, confidence=signal.extraction_confidence,
                              extractor_version="remote-message-normalizer-v1", body=signal.evidence_text,
                              published_at=published_at, available_at=available_at)
        count += 1
    for label in extract_topics(body):
        _insert_message_claim(connection, evidence_id=evidence_id, analyst_id=analyst_id, message_id=message_id,
                              scope="theme", subject_key=normalize_topic_key(label), subject_label=label, direction=direction,
                              strength=strength, horizon=horizon_days(body), confidence=confidence,
                              extractor_version="remote-message-topic-normalizer-v1", body=body,
                              published_at=published_at, available_at=available_at)
        count += 1
    if is_market_opinion(body):
        _insert_message_claim(connection, evidence_id=evidence_id, analyst_id=analyst_id, message_id=message_id,
                              scope="market", subject_key="CN_A_MARKET", subject_label="A股整体", direction=direction,
                              strength=strength, horizon=horizon_days(body), confidence=confidence,
                              extractor_version="remote-message-market-normalizer-v1", body=body,
                              published_at=published_at, available_at=available_at)
        count += 1
    return count


def import_remote_analyst_message(db: Database, message: dict[str, Any]) -> dict[str, Any]:
    """Import remote extracted text; never retrieve the referenced media.

    ``received_at`` is validated against the remote compatibility field and is
    persisted as the claim's one and only strategy availability timestamp.
    A later content version is retained in the version ledger but intentionally
    does not rewrite previously usable evidence or its point-in-time claims.
    """
    message = text_only_remote_message(message)
    message_id = str(message.get("message_id") or "").strip()
    analyst_id = str(message.get("analyst_id") or "").strip()
    source_item_id = str(message.get("source_item_id") or "").strip()
    version = str(message.get("version") or "").strip()
    content_hash = str(message.get("content_hash") or "").strip()
    received_at = parse_optional_timestamp(message.get("received_at"))
    strategy_available_at = parse_optional_timestamp(message.get("strategy_available_at"))
    if not message_id or not analyst_id or not source_item_id or not version or not content_hash or received_at is None:
        raise ValueError("remote message requires message_id, analyst_id, source_item_id, received_at, version and content_hash")
    if strategy_available_at is None or strategy_available_at != received_at:
        raise ValueError("remote message strategy_available_at must equal immutable received_at")
    source_type = str(message.get("type") or "text")
    if source_type not in {"text", "url", "image_ocr", "audio", "video"}:
        raise ValueError("remote message has an invalid type")
    published_at = parse_optional_timestamp(message.get("published_at"))
    edited_at = parse_optional_timestamp(message.get("edited_at"))
    stated_at = parse_optional_timestamp(message.get("stated_at"))
    stated_precision = message.get("stated_precision")
    if stated_precision not in (None, "minute", "second"):
        raise ValueError("remote message has an invalid stated_precision")
    body = str(message.get("content") or "")
    time_evidence = dict(message.get("time_evidence") or {})
    # Structured remote timestamps take precedence.  Legacy records can carry
    # a dated event timestamp in their text body; retain it only as author-time
    # replay evidence, never as the live strategy availability time.
    if stated_at is None:
        recovered = body_stated_timestamp(body, received_at=received_at)
        if recovered is not None:
            stated_at, stated_precision, recovered_evidence = recovered
            time_evidence = {**recovered_evidence, **time_evidence}
    with db.transaction() as connection:
        previous = connection.execute("SELECT content_hash,received_at FROM quant.remote_analyst_messages WHERE remote_message_id=%s", (message_id,)).fetchone()
        if previous is not None and previous["received_at"] != received_at:
            raise ValueError("remote message received_at changed after first receipt")
        connection.execute("INSERT INTO quant.remote_analysts(remote_analyst_id,name,remote_metadata,synced_at) VALUES(%s,%s,%s,now()) ON CONFLICT(remote_analyst_id) DO UPDATE SET synced_at=now()",
                           (analyst_id, analyst_id, Json({"source": "remote_messages"})))
        if previous is not None and str(previous["content_hash"]) != content_hash:
            connection.execute("INSERT INTO quant.remote_analyst_message_versions(remote_message_id,remote_version,content_hash,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(remote_message_id,remote_version,content_hash) DO UPDATE SET last_seen_at=now(),payload=EXCLUDED.payload",
                               (message_id, version, content_hash, Json(message)))
            return {"status": "versioned_replay_only", "remote_message_id": message_id, "evidence": 0, "claims": 0}
        # The remote archive can add source timestamp evidence after an older
        # client has first imported a message (for example, after a source
        # adapter learns the upstream message id or publish time).  Preserve
        # the immutable receipt/strategy time, but hydrate previously-missing
        # provenance fields on a later identical-content sync.  In particular
        # this must never backdate ``strategy_available_at``: source author
        # time is usable for a separate replay ledger, not for live signals.
        connection.execute(
            """INSERT INTO quant.remote_analyst_messages(remote_message_id,remote_analyst_id,source_item_id,source_message_id,source_entry_id,source_type,source_ref,
                   content,content_hash,remote_version,received_at,strategy_available_at,source_published_at,source_edited_at,stated_at,stated_precision,time_evidence,payload,synced_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT(remote_message_id) DO UPDATE SET
                   synced_at=now(),
                   source_message_id=COALESCE(quant.remote_analyst_messages.source_message_id, EXCLUDED.source_message_id),
                   source_entry_id=COALESCE(quant.remote_analyst_messages.source_entry_id, EXCLUDED.source_entry_id),
                   source_published_at=COALESCE(quant.remote_analyst_messages.source_published_at, EXCLUDED.source_published_at),
                   source_edited_at=COALESCE(quant.remote_analyst_messages.source_edited_at, EXCLUDED.source_edited_at),
                   stated_at=COALESCE(quant.remote_analyst_messages.stated_at, EXCLUDED.stated_at),
                   stated_precision=COALESCE(quant.remote_analyst_messages.stated_precision, EXCLUDED.stated_precision),
                   time_evidence=quant.remote_analyst_messages.time_evidence || EXCLUDED.time_evidence,
                   payload=EXCLUDED.payload""",
            (message_id, analyst_id, source_item_id, message.get("source_message_id"), message.get("source_entry_id"), source_type,
             str(message.get("source_ref") or ""), body, content_hash, version, received_at, received_at, published_at, edited_at,
             stated_at, stated_precision, Json(time_evidence), Json(message)),
        )
        connection.execute("INSERT INTO quant.remote_analyst_message_versions(remote_message_id,remote_version,content_hash,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(remote_message_id,remote_version,content_hash) DO UPDATE SET last_seen_at=now(),payload=EXCLUDED.payload",
                           (message_id, version, content_hash, Json(message)))
        evidence = connection.execute(
            """INSERT INTO quant.analyst_evidence(remote_message_id,evidence_key,evidence_type,body,location,content_sha256,available_at)
               VALUES(%s,'content','message',%s,%s,%s,%s) ON CONFLICT(remote_message_id,evidence_key,content_sha256) WHERE remote_message_id IS NOT NULL
               DO UPDATE SET available_at=EXCLUDED.available_at RETURNING evidence_id""",
            (message_id, body, Json({"source_type": source_type, "source_ref": str(message.get("source_ref") or ""),
                                     "stated_at": stated_at.isoformat() if stated_at else None,
                                     "time_evidence": time_evidence}), text_hash(body), received_at),
        ).fetchone()
        claims = _materialize_message_claims(connection, evidence_id=evidence["evidence_id"], analyst_id=analyst_id,
                                             message_id=message_id, body=body, published_at=published_at, available_at=received_at)
        extraction_run_id = persist_extraction_run(
            connection, analyst_id=analyst_id, source_kind="message", source_id=message_id,
            source_version=version, content_hash=content_hash, candidate_count=claims,
            accepted_count=claims,
        )
        observations = persist_observations_for_evidence(
            connection, evidence_id=evidence["evidence_id"], extraction_run_id=extraction_run_id,
            analyst_id=analyst_id, source_kind="message", source_id=message_id,
            source_version=version, content_hash=content_hash, received_at=received_at,
            strategy_available_at=received_at, published_at=published_at, edited_at=edited_at,
            stated_at=stated_at, stated_precision=stated_precision,
        )
        trade_actions = sync_anqiang_message_trade_actions(connection, message, available_at=received_at, stated_at=stated_at)
        # A message is a first-class text-only evidence source.  Refresh the
        # analyst's descriptive skill card from the unified report+message
        # corpus; it still has no authority over live strategy weights.
        rebuild_analyst_skill_profile(connection, analyst_id, received_at.astimezone(ZoneInfo("Asia/Shanghai")).date())
    return {"status": "unchanged" if previous is not None else "updated", "remote_message_id": message_id, "evidence": 1,
            "claims": claims, "observations": observations, "trade_actions": trade_actions,
            "strategy_available_at": received_at.isoformat()}


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
    published_at = parse_timestamp(report.get("updated_at") or report.get("created_at"))
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
             str(analyst.get("description") or ""), published_at, Json(analyst)),
        )
        previous = connection.execute(
            "SELECT remote_version,content_hash,raw_markdown,sections,materials FROM quant.remote_reports WHERE remote_report_id=%s", (report_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO quant.remote_reports(remote_report_id,remote_analyst_id,report_date,title,summary,source_url,remote_version,content_hash,
                 remote_created_at,remote_updated_at,remote_published_at,raw_markdown,sections,materials,mentioned_stocks,mentioned_sectors,predictions,payload,synced_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT(remote_report_id) DO UPDATE SET remote_analyst_id=EXCLUDED.remote_analyst_id,report_date=EXCLUDED.report_date,
                 title=EXCLUDED.title,summary=EXCLUDED.summary,source_url=EXCLUDED.source_url,remote_version=EXCLUDED.remote_version,
                 content_hash=EXCLUDED.content_hash,remote_updated_at=EXCLUDED.remote_updated_at,remote_published_at=EXCLUDED.remote_published_at,raw_markdown=EXCLUDED.raw_markdown,
                 sections=EXCLUDED.sections,materials=EXCLUDED.materials,mentioned_stocks=EXCLUDED.mentioned_stocks,
                 mentioned_sectors=EXCLUDED.mentioned_sectors,predictions=EXCLUDED.predictions,payload=EXCLUDED.payload,synced_at=now()
               RETURNING first_synced_at""",
            (report_id, analyst_id, report_date, str(report.get("title") or ""), str(report.get("summary") or ""), report.get("source_url"),
             version, content_hash, parse_timestamp(report.get("created_at")), published_at, published_at, markdown, Json(report.get("sections") or {}),
             Json(report.get("materials") or []), Json(report.get("mentioned_stocks") or []), Json(report.get("mentioned_sectors") or []),
             Json(report.get("predictions") or []), Json(report)),
        ).fetchone()
        version_row = connection.execute(
            """INSERT INTO quant.remote_report_versions(remote_report_id,remote_version,content_hash,payload)
               VALUES(%s,%s,%s,%s)
               ON CONFLICT(remote_report_id,remote_version,content_hash) DO UPDATE SET last_seen_at=now(),payload=EXCLUDED.payload
               RETURNING first_seen_at""",
            (report_id, version, content_hash, Json(report)),
        ).fetchone()
        # A correction/version is not usable before that exact version was
        # first acquired locally.  For an original report this equals the
        # report's first_synced_at; for a later edit it is deliberately later.
        available_at = version_row["first_seen_at"]
        sanitized_content_changed = previous is not None and (
            str(previous["raw_markdown"] or "") != markdown
            or dict(previous["sections"] or {}) != dict(report.get("sections") or {})
            or list(previous["materials"] or []) != []
        )
        changed = (previous is None or previous["remote_version"] != version or previous["content_hash"] != content_hash
                   or sanitized_content_changed)
        # This is a review-only ledger.  It preserves the author-stated
        # intraday timestamp separately from ``available_at`` and never
        # changes the normal stock/theme claim factor path.
        trade_actions = sync_anqiang_trade_actions(connection, report, available_at=available_at)
        rebuild_analyst_skill_profile(connection, analyst_id, report_date)
        if not changed and not force_reprocess:
            return {"status": "unchanged", "remote_report_id": report_id, "evidence": 0, "claims": 0,
                    "trade_actions": trade_actions}
        if force_reprocess or sanitized_content_changed:
            connection.execute(
                """DELETE FROM quant.analyst_claims c
                   USING quant.analyst_evidence e
                   WHERE c.evidence_id=e.evidence_id AND e.remote_report_id=%s""",
                (report_id,),
            )
            connection.execute("DELETE FROM quant.analyst_evidence WHERE remote_report_id=%s", (report_id,))
        extraction_run_id = persist_extraction_run(
            connection, analyst_id=analyst_id, source_kind="report", source_id=report_id,
            source_version=version, content_hash=content_hash, status="completed",
        )
        observations_count = 0
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
                         extraction_confidence,extractor_version,published_at,available_at,explicitness,raw)
                       VALUES(%s,%s,'stock',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                         strength=EXCLUDED.strength,extraction_confidence=EXCLUDED.extraction_confidence,published_at=EXCLUDED.published_at,
                         available_at=EXCLUDED.available_at,explicitness=EXCLUDED.explicitness,raw=EXCLUDED.raw
                       RETURNING claim_id""",
                    (row["evidence_id"], analyst_id, signal.symbol, signal.symbol, signal.direction, signal.strength, signal.horizon_days,
                     signal.extraction_confidence, REMOTE_EXTRACTOR_VERSION, published_at, available_at,
                     explicitness(signal.evidence_text, scope="stock"), Json({
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
                         extraction_confidence,extractor_version,published_at,available_at,explicitness,raw)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET direction=EXCLUDED.direction,
                         strength=EXCLUDED.strength,published_at=EXCLUDED.published_at,available_at=EXCLUDED.available_at,
                         explicitness=EXCLUDED.explicitness,raw=EXCLUDED.raw RETURNING claim_id""",
                        (row["evidence_id"], analyst_id, scope, subject_key, label, direction, strength, horizon_days(body), confidence,
                         REMOTE_TOPIC_EXTRACTOR_VERSION if scope == "theme" else REMOTE_EXTRACTOR_VERSION, published_at, available_at,
                         explicitness(body, scope=scope),
                         Json({"remote_report_id": report_id, "evidence_key": evidence_key, "source_label": label})),
                    ).fetchone()
                    persist_claim_revision(connection, analyst_id, scope, subject_key, direction, row["evidence_id"], claim["claim_id"])
                    claims_count += 1
            if is_market_opinion(body):
                claim = connection.execute(
                    """INSERT INTO quant.analyst_claims(evidence_id,remote_analyst_id,scope,subject_key,subject_label,direction,strength,horizon_days,
                         extraction_confidence,extractor_version,published_at,available_at,explicitness,raw)
                       VALUES(%s,%s,'market','CN_A_MARKET','A股整体',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(evidence_id,scope,subject_key,horizon_days,extractor_version) DO UPDATE SET
                         direction=EXCLUDED.direction,strength=EXCLUDED.strength,published_at=EXCLUDED.published_at,
                         available_at=EXCLUDED.available_at,explicitness=EXCLUDED.explicitness,raw=EXCLUDED.raw RETURNING claim_id""",
                    (row["evidence_id"], analyst_id, direction, strength, horizon_days(body), confidence,
                     "remote-market-normalizer-v1", published_at, available_at, explicitness(body, scope="market"),
                     Json({"remote_report_id": report_id, "evidence_key": evidence_key, "evidence_text": body})),
                ).fetchone()
                persist_claim_revision(connection, analyst_id, "market", "CN_A_MARKET", direction, row["evidence_id"], claim["claim_id"])
                claims_count += 1
            observations_count += persist_observations_for_evidence(
                connection, evidence_id=row["evidence_id"], extraction_run_id=extraction_run_id,
                analyst_id=analyst_id, source_kind="report", source_id=report_id,
                source_version=version, content_hash=content_hash, received_at=available_at,
                strategy_available_at=available_at, published_at=published_at,
                edited_at=parse_optional_timestamp(report.get("edited_at")), stated_at=None,
                stated_precision=None,
            )
    return {"status": "updated", "remote_report_id": report_id, "evidence": evidence_count, "claims": claims_count,
            "observations": observations_count, "trade_actions": trade_actions}


def reprocess_remote_reports(db: Database, limit: int = 100) -> dict[str, Any]:
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT payload FROM quant.remote_reports ORDER BY synced_at DESC LIMIT %s",
            (max(1, min(limit, 500)),),
        ).fetchall()
    results = [import_remote_report(db, dict(row["payload"]), force_reprocess=True) for row in rows]
    with db.transaction() as connection:
        skill_profiles = rebuild_all_analyst_skill_profiles(
            connection, datetime.now(ZoneInfo("Asia/Shanghai")).date(),
        )
        research = rebuild_analyst_research(connection, datetime.now(ZoneInfo("Asia/Shanghai")).date())
    return {
        "status": "completed",
        "reports": len(results),
        "evidence": sum(int(item.get("evidence", 0)) for item in results),
        "claims": sum(int(item.get("claims", 0)) for item in results),
        "items": results,
        "skill_profiles": len(skill_profiles["profiles"]),
        "research": research,
    }


def reprocess_remote_messages(db: Database, limit: int = 100) -> dict[str, Any]:
    """Rebuild message claims from stored text only; no remote fetch or media I/O."""
    with db.transaction() as connection:
        rows = connection.execute(
            "SELECT payload FROM quant.remote_analyst_messages ORDER BY received_at DESC LIMIT %s",
            (max(1, min(limit, 500)),),
        ).fetchall()
    results = [import_remote_analyst_message(db, dict(row["payload"])) for row in rows]
    with db.transaction() as connection:
        skill_profiles = rebuild_all_analyst_skill_profiles(
            connection, datetime.now(ZoneInfo("Asia/Shanghai")).date(),
        )
        research = rebuild_analyst_research(connection, datetime.now(ZoneInfo("Asia/Shanghai")).date())
    return {"status": "completed", "messages": len(results), "evidence": sum(int(item.get("evidence", 0)) for item in results),
            "claims": sum(int(item.get("claims", 0)) for item in results), "items": results,
            "skill_profiles": len(skill_profiles["profiles"]), "research": research}


def remote_report_list_state(db: Database) -> dict[str, Any]:
    with db.transaction() as connection:
        rows = connection.execute(
            """SELECT a.remote_analyst_id,
                      count(DISTINCT r.remote_report_id)::int reports,max(r.report_date) latest_report_date,max(r.synced_at) last_report_synced_at,
                      count(DISTINCT m.remote_message_id)::int messages,max(m.received_at) latest_message_received_at,max(m.synced_at) last_message_synced_at
                 FROM quant.remote_analysts a
                 LEFT JOIN quant.remote_reports r ON r.remote_analyst_id=a.remote_analyst_id
                 LEFT JOIN quant.remote_analyst_messages m ON m.remote_analyst_id=a.remote_analyst_id
                GROUP BY a.remote_analyst_id ORDER BY a.remote_analyst_id"""
        ).fetchall()
    return {"analysts": rows}


def analyst_sync_cursor(db: Database, stream_key: str, analyst_id: str) -> dict[str, Any]:
    if stream_key not in {"messages", "reports"}:
        raise ValueError("stream_key must be messages or reports")
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT stream_key,remote_analyst_id,received_at,message_ids,report_versions,updated_at
                 FROM quant.analyst_sync_cursors WHERE stream_key=%s AND remote_analyst_id=%s""",
            (stream_key, analyst_id),
        ).fetchone()
    cursor = dict(row) if row else {"stream_key": stream_key, "remote_analyst_id": analyst_id,
                                    "received_at": None, "message_ids": [], "report_versions": {}, "updated_at": None}
    # Keep a top-level copy for n8n expressions.  The nested form stays for
    # callers that already treat this as a read-model envelope.
    return {"cursor": cursor, **cursor}


def analyst_global_sync_cursor(db: Database, stream_key: str) -> dict[str, Any]:
    """Read one opaque remote change-feed cursor without inferring its value.

    The remote archive signs and owns the cursor.  On the first request the
    caller instead supplies a bounded ``received_after`` bootstrap timestamp;
    on every later request it sends only this stored token.
    """
    if stream_key != "message_updates":
        raise ValueError("stream_key must be message_updates")
    with db.transaction() as connection:
        row = connection.execute(
            """SELECT stream_key,remote_cursor,received_after,updated_at
                 FROM quant.analyst_global_sync_cursors WHERE stream_key=%s""",
            (stream_key,),
        ).fetchone()
    cursor = dict(row) if row else {
        "stream_key": stream_key, "remote_cursor": None,
        "received_after": None, "updated_at": None,
    }
    return {"cursor": cursor, **cursor}
