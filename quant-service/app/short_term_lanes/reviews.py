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


def validate(review: dict, day: date, *, require_selection: bool = True) -> None:
    if not mainboard(review):
        raise ValueError("Review must identify a non-ST mainboard stock")
    for key in ("business", "risk", "conclusion", "sources"):
        if not review.get(key):
            raise ValueError(f"Missing reviewed evidence: {key}")
    if require_selection and not valid_selection(review):
        raise ValueError('Review requires selection origin, reason, question and disposition; user follow-up requires a request reference')
    for source in review["sources"]:
        host = urlparse(source["url"]).hostname or ""
        official = any(host == domain or host.endswith('.'+domain) for domain in ("cninfo.com.cn", "sse.com.cn", "szse.cn", "cnstock.com"))
        filing_mirror = host.endswith("finance.sina.com.cn") and "/corp/view/vCB_AllBulletinDetail.php" in source["url"]
        if not official and not filing_mirror:
            raise ValueError("Review citations must be primary filings or their identified disclosure mirrors")
        if date.fromisoformat(source["published_date"]) > day:
            raise ValueError("Future evidence cannot support this scan")


def persist(database, day: date, reviews: list[dict]) -> int:
    for review in reviews:
        validate(review, day)
    now = datetime.now(timezone.utc)
    with database.transaction() as c:
        for review in reviews:
            payload = json.dumps({**review, "review_date": str(day)}, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(payload.encode()).hexdigest()
            c.execute("""INSERT INTO quant.market_events
                (event_id,symbol,event_type,occurred_at,available_at,source,title,body,url,content_sha256,availability_basis)
                VALUES(%s,%s,'short_term_company_review',%s,%s,'primary_review',%s,%s,%s,%s,'actual_review_receipt')
                ON CONFLICT(content_sha256) DO NOTHING""",
                (uuid4(), review["symbol"], datetime.combine(day,datetime.min.time(),ZoneInfo('Asia/Shanghai')),
                 now, review["name"]+"短线证据复核", payload, review["sources"][0]["url"], digest))
    return len(reviews)


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
