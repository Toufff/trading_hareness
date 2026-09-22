#!/usr/bin/env python3
"""Check one opening stage and optionally deliver its Feishu receipt."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quant-service"))

from app.alert_transport import post_feishu_alert_card  # noqa: E402
from app.database import Database  # noqa: E402
from app.http_clients import close_http_clients  # noqa: E402
from app.intraday_advisory.opening_guard import (  # noqa: E402
    FAILED, evaluate_opening_guard, opening_guard_card,
)
from app.intraday_advisory.notice_policy import guard_notification  # noqa: E402
from app.market_session_repository import sse_calendar_status  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("preopen", "live"), required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5681")
    parser.add_argument("--env-file", default=r"G:\StockPlatform\config\runtime.env")
    parser.add_argument("--as-of", help="timezone-aware ISO instant; test/manual verification only")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--recovery-attempted", action="store_true")
    parser.add_argument("--notification-state", help="durable fault receipt; defaults beside runtime logs")
    return parser.parse_args()


def _as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--as-of must include an explicit timezone offset")
    return parsed.astimezone(SHANGHAI)


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    try:
        response = client.get(path)
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {"status": "invalid_payload"}
    except (httpx.HTTPError, ValueError) as error:
        return {"status": "unavailable", "error": str(error)[:300]}


async def _notify(card: dict[str, Any]) -> dict[str, Any]:
    try:
        return await post_feishu_alert_card(card)
    finally:
        await close_http_clients()


def main() -> int:
    args = _arguments()
    load_dotenv(args.env_file, override=True)
    now = _as_of(args.as_of)
    database = Database()
    try:
        try:
            calendar_open, calendar_reason = sse_calendar_status(database, now.date())
        except Exception as error:  # fail closed but keep enough context to alert
            calendar_open, calendar_reason = None, f"calendar lookup failed: {str(error)[:240]}"
    finally:
        database.close()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=8.0, trust_env=False) as client:
        health = _get(client, "/health")
        advisory = _get(client, "/api/v1/intraday/advisory/status")
        discipline = _get(client, "/api/v1/discipline/alerts/status")

    verdict = evaluate_opening_guard(
        stage=args.stage, checked_at=now, calendar_open=calendar_open,
        calendar_reason=calendar_reason, health=health, advisory=advisory,
        discipline=discipline, recovery_attempted=args.recovery_attempted,
    )
    payload = verdict.as_dict()
    payload['notification'] = {'status':'silent'}
    if args.notify and not args.as_of:
        state_path = Path(args.notification_state) if args.notification_state else (
            Path(args.env_file).resolve().parent.parent / 'logs/runtime/opening-guard-notification.json')
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        decision = guard_notification(verdict,state)
        if decision:
            delivery = asyncio.run(_notify(opening_guard_card(verdict)))
            payload["notification"] = {"status": delivery.get("status"), 'kind':decision,
                                       "error": str(delivery.get("error") or delivery.get("reason") or "")[:300] or None}
            if delivery.get('status') == 'sent':
                state = {'open_fault': sorted({x['name'] for x in verdict.failed_checks} |
                         set(state.get('open_fault') or [])) if decision=='failure' else [],
                         'notified_at':now.isoformat()}
                state_path.parent.mkdir(parents=True,exist_ok=True)
                temp = state_path.with_suffix('.tmp')
                temp.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
                temp.replace(state_path)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if verdict.status == FAILED:
        return 2
    if payload['notification']['status'] not in {'sent','silent'}:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
