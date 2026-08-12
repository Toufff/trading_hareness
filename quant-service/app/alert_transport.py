"""Bounded opt-in delivery adapter for human-review notifications."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .http_clients import alert_http_client
from .tushare_providers import safe_error_detail


async def post_feishu_alert_text(text: str) -> dict[str, Any]:
    """Deliver only when the local Feishu adapter and token are configured."""
    webhook_url = (os.getenv("QUANT_ALERT_WEBHOOK_URL") or "").strip()
    webhook_token = (os.getenv("QUANT_ALERT_WEBHOOK_TOKEN") or "").strip()
    if not webhook_url or not webhook_token:
        return {"status": "disabled", "reason": "alert webhook or token is not configured"}
    try:
        async with alert_http_client() as client:
            response = await client.post(
                webhook_url,
                headers={"X-Quant-Alert-Token": webhook_token},
                json={"text": text},
            )
            response.raise_for_status()
            return {"status": "sent", "response": response.json()}
    except (httpx.HTTPError, ValueError) as error:
        return {"status": "failed", "error": safe_error_detail(str(error), 500)}
