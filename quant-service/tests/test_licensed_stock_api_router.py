from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.routers.licensed_stock_api import build_licensed_stock_api_router
from app.security import licensed_stock_read_allowed


def client(*, key: str = "peer-key", enabled: bool = True) -> tuple[TestClient, list[dict]]:
    calls: list[dict] = []

    async def invoke(request: dict):
        calls.append(request)
        return {
            "target": request["target"],
            "calls": 1,
            "pages": [{"payload": {"ok": True}}],
        }

    app = FastAPI()
    app.include_router(build_licensed_stock_api_router(
        configured=lambda: enabled,
        shared_read_key=lambda: key,
        call=invoke,
    ))
    return TestClient(app), calls


def test_full_catalog_requires_shared_key_and_lists_unrestricted_operations():
    http, _ = client()
    assert http.get("/licensed/stock-api/catalog").status_code == 401

    response = http.get(
        "/licensed/stock-api/catalog",
        headers={"X-Quant-Read-Key": "peer-key"},
    )
    assert response.status_code == 200
    assert response.json()["operation_restriction"] == "none_within_registered_targets"


def test_call_forwards_arbitrary_documented_action_and_batch_contract():
    http, calls = client()
    response = http.post(
        "/licensed/stock-api/call",
        headers={"X-Quant-Read-Key": "peer-key"},
        json={
            "target": "longhu_history",
            "params": {
                "a": "GGList_JGCC",
                "c": "ZhuLiChiCang",
                "st": 800,
            },
            "batch": {
                "param": "StockIDs",
                "values": ["600000", "600001"],
            },
        },
    )
    assert response.status_code == 200
    assert calls[0]["params"]["a"] == "GGList_JGCC"
    assert calls[0]["params"]["st"] == 800
    assert calls[0]["batch"]["values"] == ["600000", "600001"]


def test_batch_values_over_300_is_rejected_before_reaching_the_provider():
    http, calls = client()
    response = http.post(
        "/licensed/stock-api/call",
        headers={"X-Quant-Read-Key": "peer-key"},
        json={
            "target": "longhu_history",
            "params": {"a": "GGList_JGCC", "c": "ZhuLiChiCang"},
            "batch": {"param": "StockIDs", "values": [f"{index:06d}" for index in range(301)]},
        },
    )
    assert response.status_code == 422
    assert calls == []


def test_disabled_provider_is_explicit_service_failure():
    http, _ = client(enabled=False)
    response = http.get(
        "/licensed/stock-api/catalog",
        headers={"X-Quant-Read-Key": "peer-key"},
    )
    assert response.status_code == 503


def test_post_read_exception_is_narrow_and_requires_the_read_key():
    def request(path: str, key: str | None) -> Request:
        headers = [] if key is None else [(b"x-quant-read-key", key.encode())]
        return Request({
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers,
        })

    assert licensed_stock_read_allowed(
        request("/licensed/stock-api/call", "peer-key"), "peer-key",
    )
    assert not licensed_stock_read_allowed(
        request("/licensed/stock-api/call", "wrong"), "peer-key",
    )
    assert not licensed_stock_read_allowed(
        request("/api/v1/bootstrap", "peer-key"), "peer-key",
    )


def test_licensed_read_rejects_non_ascii_keys_instead_of_raising():
    # ``secrets.compare_digest`` raises TypeError on non-ASCII str
    # arguments, which used to surface as an unhandled 500 instead of a
    # normal rejection.  Starlette decodes header bytes as latin-1, so use
    # accented characters in that range to round-trip exactly.
    def request(path: str, key: str | None) -> Request:
        headers = [] if key is None else [(b"x-quant-read-key", key.encode("latin-1"))]
        return Request({"type": "http", "method": "POST", "path": path, "headers": headers})

    assert not licensed_stock_read_allowed(
        request("/licensed/stock-api/call", "clé-erronée"), "clé-secrète",
    )
    assert licensed_stock_read_allowed(
        request("/licensed/stock-api/call", "clé-secrète"), "clé-secrète",
    )
