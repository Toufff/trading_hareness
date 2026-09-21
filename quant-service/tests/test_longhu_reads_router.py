from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.longhu_reads import build_longhu_reads_router
import pytest


def client(*, key: str = "peer-key", enabled: bool = True, calls=None, batch=None) -> TestClient:
    async def quotes(symbols, max_symbols):
        if calls is not None:
            calls.append((list(symbols), max_symbols))
        return ([{"ts_code": symbol, "price": 10.0} for symbol in symbols],
                {"status": "completed", "max_symbols": max_symbols})

    async def minutes(symbol):
        return [{"symbol": symbol, "time": "0930", "close": 10.0}]

    app = FastAPI()
    app.include_router(build_longhu_reads_router(
        configured=lambda: enabled, shared_read_key=lambda: key,
        quotes=quotes, minutes=minutes, minutes_batch=batch,
    ))
    return TestClient(app)


def test_gateway_requires_its_separate_read_key():
    response = client().get("/licensed/longhu/quotes?symbols=600664.SH")
    assert response.status_code == 401


def test_gateway_returns_audited_cap_and_rows():
    response = client().get(
        "/licensed/longhu/quotes?symbols=600664.SH,600487.SH",
        headers={"X-Quant-Read-Key": "peer-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["rows"]) == 2
    assert payload["physical_request_limit"] == 300
    assert payload["physical_calls"] == 1
    assert payload["requested_symbols"] == 2
    assert payload["source_status"]["max_symbols"] == 300


def test_gateway_splits_more_than_300_symbols_and_combines_rows():
    calls = []
    symbols = ",".join(f"{index:06d}.SZ" for index in range(650))
    response = client(calls=calls).get(
        f"/licensed/longhu/quotes?symbols={symbols}",
        headers={"X-Quant-Read-Key": "peer-key"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["rows"]) == 650
    assert payload["physical_calls"] == 3
    assert payload["requested_symbols"] == 650
    assert [len(symbol_page) for symbol_page, _ in calls] == [300, 300, 50]
    assert all(max_symbols == 300 for _, max_symbols in calls)
    assert payload["source_status"]["status"] == "completed"
    assert payload["source_status"]["physical_calls"] == 3


HEADERS = {"X-Quant-Read-Key": "peer-key"}


async def batch(symbols, deadline):
    return {symbols[0]: [{"time": "0931", "trade_date": "20260922"}],
            **({symbols[1]: "minute_batch_deadline_exceeded"} if len(symbols)>1 else {})}


def test_minutes_batch_matches_peer_49ecf02_contract_and_normalizes_symbols():
    response = client(batch=batch).get(
        "/licensed/longhu/minutes?symbols=000001.sz,600519.SH,000002.SZ,000001.SZ&deadline_seconds=4", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert list(body["rows"]) == ["000001.SZ"]
    assert body["errors"] == {"600519.SH": "minute_batch_deadline_exceeded", "000002.SZ": "minute_batch_missing_symbol"}
    assert (body["requested"], body["completed"], body["deadline_seconds"]) == (3, 1, 4)
    assert body["session_guard"] == "current_exchange_session"
    assert body["physical_request_limit"] == 300


@pytest.mark.parametrize("query", ["symbols=", "symbols=,,,,,,", "symbols=600519.BAD", "symbols=600519.SH&deadline_seconds=0",
                                      "symbols=600519.SH&deadline_seconds=21", "symbols=600519.SH&deadline_seconds=nan",
                                      "symbols="+",".join(f"{i:06}.SZ" for i in range(301))])
def test_batch_validation_does_not_call_provider(query):
    async def never(*args):
        pytest.fail("invalid request reached provider")
    assert client(batch=never).get("/licensed/longhu/minutes?"+query, headers=HEADERS).status_code == 422


@pytest.mark.parametrize("key,enabled,headers,status", [("peer-key",True,{},401), ("peer-key",True,{"X-Quant-Read-Key":"wrong"},401),
                                                        ("",True,HEADERS,503), ("peer-key",False,HEADERS,503)])
def test_batch_authentication_and_configuration(key, enabled, headers, status):
    async def never(*args):
        pytest.fail("unauthorized request reached provider")
    assert client(key=key,enabled=enabled,batch=never).get("/licensed/longhu/minutes?symbols=600519.SH",headers=headers).status_code == status


def test_batch_saturation_is_retryable_and_safe():
    from app.runtime_executors import ExecutorSaturatedError
    async def busy(*args):
        raise ExecutorSaturatedError("private transport details")
    response = client(batch=busy).get("/licensed/longhu/minutes?symbols=600519.SH",headers=HEADERS)
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "private" not in response.text


def test_batch_provider_failure_does_not_leak_credentials():
    async def fail(*args):
        raise RuntimeError("https://upstream/?Token=SECRET")
    response = client(batch=fail).get("/licensed/longhu/minutes?symbols=600519.SH",headers=HEADERS)
    assert response.status_code == 502
    assert "SECRET" not in response.text


@pytest.mark.parametrize("encoding,compressed", [("gzip",True),("gzip;q=0",False),("identity",False),("br, gzip;q=0.5",True)])
def test_batch_gzip_negotiation(encoding, compressed):
    async def big(symbols, deadline):
        return {symbols[0]: [{"payload": "a"*70000}]}
    response = client(batch=big).get("/licensed/longhu/minutes?symbols=600519.SH",headers={**HEADERS,"Accept-Encoding":encoding})
    assert (response.headers.get("content-encoding")=="gzip") == compressed
    assert response.headers["vary"] == "Accept-Encoding"
    assert len(response.json()["rows"]["600519.SH"][0]["payload"]) == 70000


def test_existing_single_minutes_route_is_unchanged():
    response = client(batch=batch).get("/licensed/longhu/minutes/600519.SH",headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["symbol"] == "600519.SH"
    assert isinstance(response.json()["rows"], list)
