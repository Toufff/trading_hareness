"""Verify the edge application's Feishu credentials and optional target access.

This command never sends an alert.  A ``chat_id`` target can be verified with
the read-only chat endpoint.  User identifiers are deliberately token-only in
this preflight because validating them would require broader contact scopes.
"""

from __future__ import annotations

import asyncio
import argparse
import json

import httpx

from app.feishu_direct_alert import (
    FeishuTenantTokenCache,
    direct_feishu_alert_config,
    post_direct_feishu_alert_text,
)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args()
    config = direct_feishu_alert_config()
    if config is None:
        raise RuntimeError("direct Feishu alert transport is not fully configured")
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        token = await FeishuTenantTokenCache().token(client, config)
        result = {
            "status": "passed",
            "token_status": "ok",
            "receive_id_type": config.receive_id_type,
            "target_check": "not_applicable",
        }
        if config.receive_id_type == "chat_id":
            response = await client.get(
                f"https://open.feishu.cn/open-apis/im/v1/chats/{config.receive_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
            if int(payload.get("code") or 0) != 0:
                raise RuntimeError(f"Feishu chat access rejected: {str(payload.get('msg') or 'unknown')[:200]}")
            result["target_check"] = "chat_readable"
            result["chat_http_status"] = response.status_code
        else:
            result["target_check"] = "token_only_user_target"
        result["test_message_sent"] = False
        if args.send_test:
            delivery = await post_direct_feishu_alert_text(
                "[StockPlatform] 飞书告警通道验收成功；这是一条人工触发的测试消息，不是交易信号。"
            )
            if delivery.get("status") != "sent":
                raise RuntimeError(f"Feishu test delivery failed: {str(delivery.get('error') or delivery)[:200]}")
            result["test_message_sent"] = True
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
