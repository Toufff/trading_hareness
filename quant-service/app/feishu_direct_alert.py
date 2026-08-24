"""Direct Feishu transport for the self-contained intraday edge runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Mapping

import httpx

from .http_clients import alert_http_client
from .tushare_providers import safe_error_detail


FEISHU_OPEN_API_BASE = "https://open.feishu.cn/open-apis"


@dataclass(frozen=True)
class DirectFeishuAlertConfig:
    app_id: str
    app_secret: str
    receive_id: str
    receive_id_type: str


def direct_feishu_alert_config(
    environ: Mapping[str, str] | None = None,
) -> DirectFeishuAlertConfig | None:
    values = environ if environ is not None else os.environ
    enabled = str(values.get("QUANT_FEISHU_DIRECT_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    if not enabled:
        return None
    app_id = str(values.get("FEISHU_APP_ID", "")).strip()
    app_secret = str(values.get("FEISHU_APP_SECRET", "")).strip()
    receive_id = str(values.get("FEISHU_ALERT_RECEIVE_ID", "")).strip()
    receive_id_type = str(values.get("FEISHU_ALERT_RECEIVE_ID_TYPE", "chat_id")).strip() or "chat_id"
    if not app_id or not app_secret or not receive_id:
        return None
    return DirectFeishuAlertConfig(app_id, app_secret, receive_id, receive_id_type)


def direct_feishu_alert_configured(environ: Mapping[str, str] | None = None) -> bool:
    return direct_feishu_alert_config(environ) is not None


class FeishuTenantTokenCache:
    """Small process-local cache; the durable alert outbox owns retries."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._app_id = ""
        self._token = ""
        self._expires_at = 0.0

    async def token(self, client: httpx.AsyncClient, config: DirectFeishuAlertConfig) -> str:
        now = asyncio.get_running_loop().time()
        if self._app_id == config.app_id and self._token and now < self._expires_at:
            return self._token
        async with self._lock:
            now = asyncio.get_running_loop().time()
            if self._app_id == config.app_id and self._token and now < self._expires_at:
                return self._token
            response = await client.post(
                f"{FEISHU_OPEN_API_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": config.app_id, "app_secret": config.app_secret},
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("code") or 0) != 0 or not payload.get("tenant_access_token"):
                raise ValueError(f"Feishu tenant token rejected: {str(payload.get('msg') or 'unknown error')[:200]}")
            self._app_id = config.app_id
            self._token = str(payload["tenant_access_token"])
            expires_in = max(120, int(payload.get("expire") or 7200))
            self._expires_at = now + expires_in - 60
            return self._token


_tenant_token_cache = FeishuTenantTokenCache()


async def post_direct_feishu_alert_text(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] = alert_http_client,
    token_cache: FeishuTenantTokenCache = _tenant_token_cache,
) -> dict[str, Any]:
    """Send one text notification with the application identity on the edge."""
    config = direct_feishu_alert_config(environ)
    if config is None:
        return {"status": "disabled", "reason": "direct Feishu alert transport is not fully configured"}
    try:
        async with client_factory() as client:
            token = await token_cache.token(client, config)
            response = await client.post(
                f"{FEISHU_OPEN_API_BASE}/im/v1/messages",
                params={"receive_id_type": config.receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": config.receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": str(text)}, ensure_ascii=False),
                },
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("code") or 0) != 0:
                raise ValueError(f"Feishu message rejected: {str(payload.get('msg') or 'unknown error')[:200]}")
            return {"status": "sent", "response": payload}
    except (httpx.HTTPError, ValueError, TypeError) as error:
        return {"status": "failed", "error": safe_error_detail(str(error), 500)}


__all__ = [
    "DirectFeishuAlertConfig", "FeishuTenantTokenCache", "direct_feishu_alert_config",
    "direct_feishu_alert_configured", "post_direct_feishu_alert_text",
]
