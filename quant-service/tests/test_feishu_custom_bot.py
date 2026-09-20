import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.alert_transport import post_feishu_alert_text
from app.feishu_custom_bot import custom_bot_configured, custom_bot_signature, post_custom_bot_text


WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/example-token"
CUSTOM_BOT_ENV = {
    "FEISHU_ALERTS_CONFIGURED": "true",
    "FEISHU_ALERT_TRANSPORT": "custom_bot",
    "FEISHU_CUSTOM_BOT_WEBHOOK_URL": WEBHOOK,
}


class FeishuCustomBotTests(unittest.TestCase):
    def test_configuration_requires_an_official_https_hook(self):
        self.assertFalse(custom_bot_configured({}))
        self.assertFalse(custom_bot_configured({"FEISHU_CUSTOM_BOT_WEBHOOK_URL": "http://example.test/hook"}))
        self.assertTrue(custom_bot_configured(CUSTOM_BOT_ENV))

    def test_stale_url_is_inert_when_disabled_or_app_mode_is_selected(self):
        self.assertFalse(custom_bot_configured({
            **CUSTOM_BOT_ENV, "FEISHU_ALERTS_CONFIGURED": "false",
        }))
        self.assertFalse(custom_bot_configured({
            **CUSTOM_BOT_ENV, "FEISHU_ALERT_TRANSPORT": "app",
        }))

    def test_documented_signature_contract(self):
        self.assertEqual(custom_bot_signature(1700000000, "top-secret"),
                         "k5kp1+YChJjoQSV36+S5u0IJWe3a56MRrGAVEWvaHUE=")

    def test_signed_text_payload_is_sent_without_secret_disclosure(self):
        captured = {}

        def handle(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"StatusCode": 0, "StatusMessage": "success"})

        @asynccontextmanager
        async def client_factory():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                yield client

        result = asyncio.run(post_custom_bot_text(
            "纪律线触发", environ={**CUSTOM_BOT_ENV,
                                  "FEISHU_CUSTOM_BOT_SIGNING_SECRET": "top-secret"},
            client_factory=client_factory,
            now=lambda: datetime.fromtimestamp(1700000000, timezone.utc),
        ))
        self.assertEqual(result["status"], "sent")
        self.assertEqual(captured["msg_type"], "text")
        self.assertEqual(captured["content"], {"text": "纪律线触发"})
        self.assertEqual(captured["timestamp"], "1700000000")
        self.assertEqual(captured["sign"], "k5kp1+YChJjoQSV36+S5u0IJWe3a56MRrGAVEWvaHUE=")
        self.assertNotIn("top-secret", str(result))

    def test_transport_prefers_custom_bot_before_application_mode(self):
        with patch.dict("os.environ", CUSTOM_BOT_ENV, clear=True), \
             patch("app.alert_transport.post_custom_bot_text", new=AsyncMock(return_value={"status": "sent"})) as custom, \
             patch("app.alert_transport.post_direct_feishu_alert_text", new=AsyncMock()) as direct:
            result = asyncio.run(post_feishu_alert_text("test"))
        self.assertEqual(result["status"], "sent")
        custom.assert_awaited_once_with("test")
        direct.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
