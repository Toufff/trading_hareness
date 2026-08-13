"""Deterministic, point-in-time analyst text feature aggregation."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from typing import Any, Callable
from zoneinfo import ZoneInfo


CN_TZ = ZoneInfo("Asia/Shanghai")


def analyst_text_factor_summary(connection: Any, as_of_date: date, *, classify_text: Callable[[str], tuple[int, float, float]],
                                factor_version: str, lookback_days: int = 7,
                                available_before: datetime | None = None) -> dict[str, Any]:
    """Aggregate one decayed vote per analyst/report and theme.

    ``connection`` is supplied by the caller's existing transaction.  This
    module never opens a transaction, calls a remote service, or changes the
    analyst promotion registry.
    """
    lookback_days = max(1, min(30, int(lookback_days)))
    earliest = as_of_date - timedelta(days=lookback_days - 1)
    with connection.cursor() as cursor:
        cursor.execute(
            """SELECT r.remote_analyst_id,r.remote_report_id,r.summary,r.sections,
                      r.first_synced_at AS available_at,
                      coalesce(r.remote_published_at,r.remote_updated_at,r.remote_created_at) AS published_at
                 FROM quant.remote_reports r
                WHERE (r.first_synced_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::timestamptz IS NULL OR r.first_synced_at<=%s)
                ORDER BY available_at DESC""",
            (earliest, as_of_date, available_before, available_before),
        )
        reports = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            """SELECT c.remote_analyst_id,c.subject_key,c.subject_label,c.direction,c.strength,c.extraction_confidence,
                      c.available_at,e.remote_report_id,e.evidence_key
                 FROM quant.analyst_claims c JOIN quant.analyst_evidence e ON e.evidence_id=c.evidence_id
                WHERE c.scope='theme' AND (c.available_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %s AND %s
                  AND (%s::timestamptz IS NULL OR c.available_at<=%s)
                  AND e.evidence_key = ANY(%s)""",
            (earliest, as_of_date, available_before, available_before,
             ["summary", "section:market_view", "section:operation_guidance",
              "section:future_scenarios", "section:sectors_and_stocks"]),
        )
        topic_claims = [dict(row) for row in cursor.fetchall()]

    def recency_weight(available_at: datetime | None) -> float:
        local_date = available_at.astimezone(CN_TZ).date() if available_at else as_of_date
        age = max(0, (as_of_date - local_date).days)
        return math.exp(-math.log(2) * age / 3.0)

    analyst_reports: dict[str, list[tuple[float, float]]] = {}
    for report in reports:
        sections = dict(report.get("sections") or {})
        text = "\n".join(str(value) for value in (
            report.get("summary"), sections.get("market_view"), sections.get("operation_guidance"),
            sections.get("future_scenarios"), sections.get("sectors_and_stocks"),
        ) if value)
        direction, strength, confidence = classify_text(text)
        analyst_reports.setdefault(str(report["remote_analyst_id"]), []).append(
            (float(direction) * float(strength) * float(confidence), recency_weight(report.get("available_at")))
        )
    analyst_votes = {
        analyst_id: sum(score * weight for score, weight in values) / sum(weight for _, weight in values)
        for analyst_id, values in analyst_reports.items() if sum(weight for _, weight in values) > 0
    }
    market_consensus = sum(analyst_votes.values()) / len(analyst_votes) if analyst_votes else 0.0
    market = {
        "consensus": round(market_consensus, 5), "analyst_count": len(analyst_votes), "report_count": len(reports),
        "agreement": round(abs(sum(1 if vote > 0 else -1 if vote < 0 else 0 for vote in analyst_votes.values())) / len(analyst_votes), 5) if analyst_votes else 0.0,
        "freshness": round(sum(recency_weight(report.get("available_at")) for report in reports) / len(reports), 5) if reports else 0.0,
        "votes": [{"analyst_id": analyst_id, "score": round(score, 5)} for analyst_id, score in sorted(analyst_votes.items())],
    }

    report_theme_scores: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
    theme_labels: dict[str, str] = {}
    for claim in topic_claims:
        topic_key = str(claim["subject_key"])
        theme_labels[topic_key] = str(claim.get("subject_label") or topic_key)
        key = (topic_key, str(claim["remote_analyst_id"]), str(claim["remote_report_id"]))
        report_theme_scores.setdefault(key, []).append((
            float(claim["direction"]) * float(claim["strength"]) * float(claim["extraction_confidence"]),
            recency_weight(claim.get("available_at")),
        ))
    analyst_topic_scores: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for (topic_key, analyst_id, _report_id), values in report_theme_scores.items():
        analyst_topic_scores.setdefault((topic_key, analyst_id), []).append((
            sum(item[0] for item in values) / len(values), sum(item[1] for item in values) / len(values)))
    topics_by_key: dict[str, list[tuple[str, float]]] = {}
    for (topic_key, analyst_id), values in analyst_topic_scores.items():
        total_weight = sum(item[1] for item in values)
        if total_weight:
            topics_by_key.setdefault(topic_key, []).append((analyst_id, sum(score * weight for score, weight in values) / total_weight))
    topics: list[dict[str, Any]] = []
    for topic_key, values in topics_by_key.items():
        votes = [score for _, score in values]
        consensus = sum(votes) / len(votes)
        topics.append({"topic_key": topic_key, "label": theme_labels.get(topic_key, topic_key),
                       "consensus": round(consensus, 5), "analyst_count": len(values),
                       "agreement": round(abs(sum(1 if score > 0 else -1 if score < 0 else 0 for score in votes)) / len(votes), 5),
                       "analyst_votes": [{"analyst_id": analyst_id, "score": round(score, 5)} for analyst_id, score in sorted(values)]})
    topics.sort(key=lambda item: (-abs(float(item["consensus"])) * max(1, int(item["analyst_count"])), item["label"]))
    return {"factor_version": factor_version, "as_of_date": str(as_of_date), "lookback_days": lookback_days,
            "market": market, "themes": topics,
            "data_boundary": "text-only remote report fields; report-level and analyst-level deduplication"}

