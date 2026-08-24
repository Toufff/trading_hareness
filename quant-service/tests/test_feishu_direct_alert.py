import asyncio
import json
import unittest
from contextlib import asynccontextmanager

import httpx

from app.feishu_direct_alert import (
    FeishuTenantTokenCache,
    direct_feishu_alert_configured,
    post_direct_feishu_alert_text,
)


class FeishuDirectAlertTests(unittest.TestCase):
    def test_direct_transport_requires_explicit_complete_configuration(self):
        self.assertFalse(direct_feishu_alert_configured({}))
        self.assertFalse(direct_feishu_alert_configured({"QUANT_FEISHU_DIRECT_ENABLED": "true"}))
        self.assertTrue(direct_feishu_alert_configured({
            "QUANT_FEISHU_DIRECT_ENABLED": "true",
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_ALERT_RECEIVE_ID": "chat-id",
        }))

    def test_direct_transport_caches_tenant_token_and_sends_text(self):
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/tenant_access_token/internal"):
                return httpx.Response(200, json={
                    "code": 0, "tenant_access_token": "tenant-token", "expire": 7200,
                })
            self.assertEqual(request.headers["Authorization"], "Bearer tenant-token")
            payload = json.loads(request.content)
            self.assertEqual(payload["receive_id"], "chat-id")
            self.assertEqual(json.loads(payload["content"]), {"text": "盘中证据"})
            return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"message_id": "m1"}})

        @asynccontextmanager
        async def client_factory():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                yield client

        environ = {
            "QUANT_FEISHU_DIRECT_ENABLED": "true",
            "FEISHU_APP_ID": "app-id",
            "FEISHU_APP_SECRET": "secret",
            "FEISHU_ALERT_RECEIVE_ID": "chat-id",
            "FEISHU_ALERT_RECEIVE_ID_TYPE": "chat_id",
        }

        async def run() -> tuple[dict, dict]:
            cache = FeishuTenantTokenCache()
            first = await post_direct_feishu_alert_text(
                "盘中证据", environ=environ, client_factory=client_factory, token_cache=cache,
            )
            second = await post_direct_feishu_alert_text(
                "盘中证据", environ=environ, client_factory=client_factory, token_cache=cache,
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "sent")
        self.assertEqual(sum(request.url.path.endswith("/tenant_access_token/internal") for request in requests), 1)
        self.assertEqual(len(requests), 3)

    def test_direct_transport_fails_closed_on_feishu_application_error(self):
        @asynccontextmanager
        async def client_factory():
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"code": 999, "msg": "denied"})
            )) as client:
                yield client

        result = asyncio.run(post_direct_feishu_alert_text(
            "test",
            environ={
                "QUANT_FEISHU_DIRECT_ENABLED": "true", "FEISHU_APP_ID": "app-id",
                "FEISHU_APP_SECRET": "secret", "FEISHU_ALERT_RECEIVE_ID": "chat-id",
            },
            client_factory=client_factory,
            token_cache=FeishuTenantTokenCache(),
        ))
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
