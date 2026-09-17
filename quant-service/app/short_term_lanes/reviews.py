"""Persist reviewed primary evidence separately from mechanical discoveries."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

from .rules import mainboard
from .selection import valid_selection

# Industry data, news and sector ETF proxies gathered for one decision are real
# research and belong in the ledger, but they never replace the filing that the
# company conclusion itself must rest on: at least one primary source stays
# mandatory and only explicitly labelled research sources skip the host check.
RESEARCH_SOURCE_KINDS = {"information_check", "sector_etf_proxy"}


def validate(review: dict, day: date, *, require_selection: bool = True) -> None:
    if not mainboard(review):
        raise ValueError("Review must identify a non-ST mainboard stock")
    for key in ("business", "risk", "conclusion", "sources"):
        if not review.get(key):
            raise ValueError(f"Missing reviewed evidence: {key}")
    if require_selection and not valid_selection(review):
        raise ValueError('Review requires selection origin, reason, question and disposition; user follow-up requires a request reference')
    primary = False
    for source in review["sources"]:
        host = urlparse(source["url"]).hostname or ""
        official = any(host == domain or host.endswith('.'+domain) for domain in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "cnstock.com"))
        filing_mirror = host.endswith("finance.sina.com.cn") and "/corp/view/vCB_AllBulletinDetail.php" in source["url"]
        research = source.get("kind") in RESEARCH_SOURCE_KINDS and str(source.get("url") or "").startswith("https://")
        if not official and not filing_mirror and not research:
            raise ValueError("Review citations must be primary filings or their identified disclosure mirrors")
        primary = primary or official or filing_mirror
        if date.fromisoformat(source["published_date"]) > day:
            raise ValueError("Future evidence cannot support this scan")
    if not primary:
        raise ValueError("Review citations must include at least one primary filing or disclosure mirror")


def persist_rows(connection, day: date, reviews: list[dict], *, now=None) -> int:
    """Persist validated research using an existing transaction."""
    for review in reviews:
        validate(review, day)
    now = now or datetime.now(timezone.utc)
    for review in reviews:
        payload = json.dumps({**review, "review_date": str(day)}, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        connection.execute("""INSERT INTO quant.market_events
            (event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,availability_basis)
            VALUES(%s,%s,'short_term_company_review',%s,%s,'primary_review',%s,%s,%s,%s,'actual_review_receipt')
            ON CONFLICT(content_sha256) DO NOTHING""",
            (uuid4(), review["symbol"], datetime.combine(day,datetime.min.time(),ZoneInfo('Asia/Shanghai')),
             now, review["name"]+"短线证据复核", payload, review["sources"][0]["url"], digest))
    return len(reviews)


def persist(database, day: date, reviews: list[dict]) -> int:
    with database.transaction() as connection:
        return persist_rows(connection, day, reviews)


def from_recommendation(items: list[dict]) -> list[dict]:
    """Adapt completed pool research into the shared company evidence ledger."""
    result = []
    dispositions = {"recommend": "retain_watch", "observe": "retain_watch", "exclude": "exclude"}
    for item in items:
        sources = item.get("sources") or []
        selection_sources = set(item.get("sources_of_selection") or [])
        if "user_tracking" in selection_sources:
            origin = "user_followup"
        elif item.get("memberships"):
            origin = "scan"
        else:
            origin = "background"
        selection = {
            "origin": origin,
            "why_now": item.get("why_now") or item.get("comparison") or "本轮推荐池续审",
            "question": item.get("peer_comparison") or item.get("comparison") or "公司事实是否支持策略观察",
            "disposition": dispositions.get(item.get("decision"), "downgrade_watch"),
        }
        if origin == "user_followup":
            selection["request_reference"] = "用户主动跟踪池续审"
        review = {
            "symbol": item["symbol"],
            "name": item.get("name") or item["symbol"],
            "business": item.get("business"),
            "risk": item.get("company_risk"),
            "conclusion": item.get("comparison") or item.get("why_now"),
            "sources": sources,
            "selection": selection,
            "recommendation_research": {
                key: item.get(key) for key in (
                    "decision", "stage", "trigger", "invalidation", "peer_comparison",
                    "sector_assessment", "priority", "data_date", "recommendation_note", "ranking_reference"
                ) if item.get(key) is not None
            },
        }
        if item.get("catalyst"):
            review["catalyst"] = item["catalyst"]
        result.append(review)
    return result


def load(database, day: date) -> dict[str, dict]:
    with database.transaction() as c:
        rows = c.execute("""SELECT DISTINCT ON(symbol) symbol,body FROM quant.market_events
            WHERE event_type='short_term_company_review' AND source='primary_review'
              AND (occurred_at AT TIME ZONE 'Asia/Shanghai')::date=%s
              AND available_at < (%s::date+1) AT TIME ZONE 'Asia/Shanghai'
            ORDER BY symbol,available_at DESC""", (day,day)).fetchall()
    result = {}
    for row in rows:
        try:
            review=json.loads(row["body"]);validate(review,day,require_selection=False);result[row["symbol"]]=review
        except (ValueError,TypeError,KeyError):
            continue
    return result
