"""Bounded opt-in delivery adapter for human-review notifications."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .http_clients import alert_http_client
from .feishu_custom_bot import custom_bot_configured, post_custom_bot_card, post_custom_bot_text
from .feishu_direct_alert import direct_feishu_alert_configured, post_direct_feishu_alert_card, post_direct_feishu_alert_text
from .tushare_providers import safe_error_detail


async def post_feishu_alert_text(text: str) -> dict[str, Any]:
    """Deliver through the edge-owned direct path or the local adapter."""
    if custom_bot_configured():
        return await post_custom_bot_text(text)
    if direct_feishu_alert_configured():
        return await post_direct_feishu_alert_text(text)
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


async def post_feishu_alert_card(card: dict[str, Any]) -> dict[str, Any]:
    """Deliver one native Feishu interactive card through a configured official transport."""
    if custom_bot_configured():
        return await post_custom_bot_card(card)
    if direct_feishu_alert_configured():
        return await post_direct_feishu_alert_card(card)
    return {"status": "disabled", "reason": "native Feishu card transport is not configured"}


def feishu_alert_transport_configured() -> bool:
    """Whether any supported Feishu notification transport is complete."""
    if custom_bot_configured() or direct_feishu_alert_configured():
        return True
    return bool(
        (os.getenv("QUANT_ALERT_WEBHOOK_URL") or "").strip() and (os.getenv("QUANT_ALERT_WEBHOOK_TOKEN") or "").strip()
    )


__all__ = ["feishu_alert_transport_configured", "post_feishu_alert_card", "post_feishu_alert_text"]
