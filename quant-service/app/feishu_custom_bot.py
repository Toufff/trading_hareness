"""Official Feishu custom-bot webhook transport.

This is the lowest-friction notification mode: one webhook URL, plus the
optional signing secret configured on the bot.  Values remain in the runtime
environment and are never included in status payloads or logs.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import os
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .http_clients import alert_http_client
from .tushare_providers import safe_error_detail


@dataclass(frozen=True)
class FeishuCustomBotConfig:
    webhook_url: str
    secret: str | None = None


def custom_bot_config(environ: Mapping[str, str] | None = None) -> FeishuCustomBotConfig | None:
    values = environ if environ is not None else os.environ
    enabled = str(values.get("FEISHU_ALERTS_CONFIGURED", "false")).strip().lower()
    transport = str(values.get("FEISHU_ALERT_TRANSPORT", "")).strip().lower()
    if enabled not in {"1", "true", "yes", "on"} or transport != "custom_bot":
        return None
    url = str(values.get("FEISHU_CUSTOM_BOT_WEBHOOK_URL") or "").strip()
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or "/open-apis/bot/v2/hook/" not in parsed.path:
        return None
    secret = str(values.get("FEISHU_CUSTOM_BOT_SIGNING_SECRET") or "").strip() or None
    return FeishuCustomBotConfig(url, secret)


def custom_bot_configured(environ: Mapping[str, str] | None = None) -> bool:
    return custom_bot_config(environ) is not None


def custom_bot_signature(timestamp: int | str, secret: str) -> str:
    """Feishu's documented ``timestamp + newline + secret`` HMAC signature."""
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


async def post_custom_bot_text(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] = alert_http_client,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    config = custom_bot_config(environ)
    if config is None:
        return {"status": "disabled", "reason": "Feishu custom-bot webhook is not configured"}
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": str(text)}}
    if config.secret:
        timestamp = int(now().timestamp())
        payload.update({"timestamp": str(timestamp), "sign": custom_bot_signature(timestamp, config.secret)})
    try:
        async with client_factory() as client:
            response = await client.post(config.webhook_url, json=payload)
            response.raise_for_status()
            body = response.json()
            code = body.get("code", body.get("StatusCode", 0))
            if int(code or 0) != 0:
                message = body.get("msg", body.get("StatusMessage", "unknown error"))
                raise ValueError(f"Feishu custom bot rejected: {str(message)[:200]}")
            return {"status": "sent", "response": body}
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return {"status": "failed", "error": safe_error_detail(str(error), 500)}


async def post_custom_bot_card(
    card: dict[str, Any], *, environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] = alert_http_client,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    config = custom_bot_config(environ)
    if config is None:
        return {"status": "disabled", "reason": "Feishu custom-bot webhook is not configured"}
    payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
    if config.secret:
        timestamp = int(now().timestamp())
        payload.update({"timestamp": str(timestamp), "sign": custom_bot_signature(timestamp, config.secret)})
    try:
        async with client_factory() as client:
            response = await client.post(config.webhook_url, json=payload)
            response.raise_for_status()
            body = response.json()
            code = body.get("code", body.get("StatusCode", 0))
            if int(code or 0) != 0:
                raise ValueError(f"Feishu custom bot rejected: {str(body.get('msg') or body.get('StatusMessage'))[:200]}")
            return {"status": "sent", "response": body}
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return {"status": "failed", "error": safe_error_detail(str(error), 500)}


__all__ = [
    "FeishuCustomBotConfig", "custom_bot_config", "custom_bot_configured",
    "custom_bot_signature", "post_custom_bot_card", "post_custom_bot_text",
]
