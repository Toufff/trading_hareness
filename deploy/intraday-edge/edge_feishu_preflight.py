"""Read-only verification of the edge application's Feishu target access."""

from __future__ import annotations

import asyncio

import httpx

from app.feishu_direct_alert import FeishuTenantTokenCache, direct_feishu_alert_config


async def main() -> None:
    config = direct_feishu_alert_config()
    if config is None:
        raise RuntimeError("direct Feishu alert transport is not fully configured")
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        token = await FeishuTenantTokenCache().token(client, config)
        response = await client.get(
            f"https://open.feishu.cn/open-apis/im/v1/chats/{config.receive_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
        print({
            "token_status": "ok",
            "chat_http_status": response.status_code,
            "chat_code": payload.get("code"),
        })


if __name__ == "__main__":
    asyncio.run(main())
