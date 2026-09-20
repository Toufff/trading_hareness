"""Send one explicit custom-bot acceptance message.

This is not used by the runtime loop. It exists only for an operator-requested
live acceptance after the webhook has been configured.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import hashlib
import hmac
import json
import os
import time

import httpx


def _sign(timestamp: int, secret: str) -> str:
    value = f"{timestamp}\n{secret}".encode("utf-8")
    return base64.b64encode(hmac.new(value, digestmod=hashlib.sha256).digest()).decode("ascii")


async def main() -> None:
    url = os.getenv("FEISHU_CUSTOM_BOT_WEBHOOK_URL", "").strip()
    secret = os.getenv("FEISHU_CUSTOM_BOT_SIGNING_SECRET", "").strip()
    if not url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/"):
        raise RuntimeError("official Feishu custom-bot webhook is not configured")
    payload: dict[str, object] = {
        "msg_type": "text",
        "content": {
            "text": "[StockPlatform] 飞书告警通道验收成功；这是一条人工触发的测试消息，不是交易信号。"
        },
    }
    if secret:
        timestamp = int(time.time())
        payload.update({"timestamp": str(timestamp), "sign": _sign(timestamp, secret)})
    async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        code = int(body.get("code") or body.get("StatusCode") or 0)
        if code != 0:
            raise RuntimeError(f"Feishu custom bot rejected the test: {str(body.get('msg') or body.get('StatusMessage') or 'unknown')[:200]}")
    print(json.dumps({
        "status": "passed",
        "transport": "custom_bot",
        "test_message_sent": True,
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
