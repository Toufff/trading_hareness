"""Exercise the exact direct-alert transport without credentials or network."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.feishu_direct_alert import FeishuTenantTokenCache, post_direct_feishu_alert_text


async def main() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        if request.url.path.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "mock-token", "expire": 7200},
            )
        if request.url.path.endswith("/im/v1/messages"):
            assert request.url.params["receive_id_type"] == "chat_id"
            assert request.headers["authorization"] == "Bearer mock-token"
            payload = json.loads(request.content)
            assert payload["receive_id"] == "oc_mock_target"
            assert payload["msg_type"] == "text"
            assert json.loads(payload["content"])["text"] == "mock discipline line triggered"
            return httpx.Response(
                200,
                json={"code": 0, "msg": "success", "data": {"message_id": "om_mock"}},
            )
        return httpx.Response(404, json={"code": 404, "msg": "unexpected mock route"})

    transport = httpx.MockTransport(handler)

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, timeout=2.0, trust_env=False)

    result = await post_direct_feishu_alert_text(
        "mock discipline line triggered",
        environ={
            "QUANT_FEISHU_DIRECT_ENABLED": "true",
            "FEISHU_APP_ID": "cli_mock",
            "FEISHU_APP_SECRET": "mock-secret",
            "FEISHU_ALERT_RECEIVE_ID": "oc_mock_target",
            "FEISHU_ALERT_RECEIVE_ID_TYPE": "chat_id",
        },
        client_factory=client_factory,
        token_cache=FeishuTenantTokenCache(),
    )
    assert result["status"] == "sent", result
    assert [request.url.path for request in observed] == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/im/v1/messages",
    ]
    print(json.dumps({"status": "passed", "network": "mocked", "requests": 2}, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
