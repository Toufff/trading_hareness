from __future__ import annotations

from dataclasses import dataclass

from app.licensed_stock_api import (
    MAX_EXPECTED_CALLS,
    MAX_PHYSICAL_BATCH,
    MAX_TIMEOUT_SECONDS,
    TARGETS,
    UpstreamStockApiError,
    catalog,
    execute,
    expected_call_count,
)


@dataclass
class Config:
    token: str = "owner-token"
    user_id: str = "owner-user"
    device_id: str = "owner-device"
    ranking_device_id: str = "ranking-device"
    version: str = "5.23.0.4"
    timeout_seconds: float = 1.0
    retries: int = 1


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FailingResponse:
    status_code = 404

    def raise_for_status(self):
        error = __import__("requests").HTTPError(
            "credential-bearing-url-must-not-escape",
            response=self,
        )
        raise error


class Session:
    def __init__(self):
        self.calls = []

    def get(self, url, *, params, timeout):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return Response({"list": [params.get("Index", params.get("index", 0))]})


def test_catalog_exposes_every_documented_family_without_operation_restriction():
    payload = catalog()
    assert len(payload["targets"]) == 8
    assert len(payload["documented_operations"]) >= 50
    assert len(payload["documented_examples"]) == 89
    assert all(
        sensitive not in {str(key).lower() for key in example["params"]}
        for example in payload["documented_examples"]
        for sensitive in ("token", "userid", "deviceid")
    )
    assert payload["physical_batch_limit"] == 300
    assert payload["operation_restriction"] == "none_within_registered_targets"


def test_every_documented_longhu_data_host_is_registered():
    assert {
        target.base_url for target in TARGETS.values()
        if target.base_url.endswith(".longhuvip.com")
    } == {
        "https://apphis.longhuvip.com",
        "https://apphwhq.longhuvip.com",
        "https://apphq.longhuvip.com",
        "https://apphwshhq.longhuvip.com",
        "https://applhb.longhuvip.com",
        "https://apparticle.longhuvip.com",
    }


def test_undocumented_action_and_custom_path_are_passed_through():
    session = Session()
    execute(
        session=session,
        config=Config(),
        target_key="longhu_quote",
        path="/w1/api/future.php",
        params={"a": "FutureLonghuAction", "c": "FutureController", "custom": "kept"},
    )
    assert session.calls[0]["url"] == "https://apphwhq.longhuvip.com/w1/api/future.php"
    assert session.calls[0]["params"]["a"] == "FutureLonghuAction"
    assert session.calls[0]["params"]["c"] == "FutureController"
    assert session.calls[0]["params"]["custom"] == "kept"


def test_large_logical_page_is_split_into_physical_300_row_calls():
    session = Session()
    payload = execute(
        session=session,
        config=Config(),
        target_key="longhu_history",
        path=None,
        params={
            "a": "DailyLimitPerformance",
            "c": "HisHomeDingPan",
            "st": 650,
            "Index": 7,
            "Token": "caller-must-not-override",
            "UserID": "caller-user",
            "DeviceID": "caller-device",
        },
    )
    assert payload["calls"] == 3
    assert payload["batched"] is True
    assert [call["params"]["st"] for call in session.calls] == [300, 300, 50]
    assert [call["params"]["Index"] for call in session.calls] == [7, 307, 607]
    assert all(call["params"]["Token"] == "owner-token" for call in session.calls)
    assert all(call["params"]["UserID"] == "owner-user" for call in session.calls)
    assert all(call["params"]["DeviceID"] == "owner-device" for call in session.calls)
    assert all(call["params"]["st"] <= MAX_PHYSICAL_BATCH for call in session.calls)


def test_large_explicit_value_batch_is_split_without_losing_values():
    session = Session()
    values = [f"{index:06d}" for index in range(650)]
    payload = execute(
        session=session,
        config=Config(),
        target_key="longhu_quote",
        path=None,
        params={"a": "GetStockPanKou", "c": "StockL2Data"},
        batch_param="StockIDs",
        batch_values=values,
    )
    assert payload["calls"] == 3
    assert [page["batch_count"] for page in payload["pages"]] == [300, 300, 50]
    received = []
    for call in session.calls:
        received.extend(call["params"]["StockIDs"].split(","))
    assert received == values


def test_ranking_action_uses_owner_ranking_identity():
    session = Session()
    execute(
        session=session,
        config=Config(),
        target_key="longhu_market",
        path=None,
        params={"a": "RealRankingInfo", "c": "ZhiShuRanking"},
    )
    assert session.calls[0]["params"]["DeviceID"] == "ranking-device"


def test_external_target_does_not_receive_owner_credentials():
    session = Session()
    execute(
        session=session,
        config=Config(),
        target_key="xuangubao",
        path="/api/pool/detail",
        params={"pool_name": "limit_up"},
    )
    params = session.calls[0]["params"]
    assert "Token" not in params
    assert "UserID" not in params
    assert "DeviceID" not in params


def test_only_registered_hosts_are_callable_and_path_traversal_is_rejected():
    session = Session()
    try:
        execute(
            session=session,
            config=Config(),
            target_key="arbitrary_host",
            path="/",
            params={},
        )
    except ValueError as error:
        assert "unknown stock API target" in str(error)
    else:
        raise AssertionError("arbitrary host was accepted")

    try:
        execute(
            session=session,
            config=Config(),
            target_key="xuangubao",
            path="/api/../private",
            params={},
        )
    except ValueError as error:
        assert "path" in str(error)
    else:
        raise AssertionError("path traversal was accepted")


def test_expected_call_count_matches_cross_product_batching():
    assert expected_call_count(requested_size=650) == 3
    assert expected_call_count(batch_value_count=601) == 3
    assert expected_call_count(requested_size=650, batch_value_count=601) == 9


def test_st_and_batch_combination_exceeding_the_call_cap_is_rejected():
    # 3000 rows needs 10 calls (at the cap); one more row pushes it to 11,
    # which must be rejected before any upstream request is made.
    session = Session()
    try:
        execute(
            session=session,
            config=Config(),
            target_key="longhu_history",
            path=None,
            params={"a": "DailyLimitPerformance", "c": "HisHomeDingPan", "st": 3001},
        )
    except ValueError as error:
        assert "upstream calls" in str(error)
    else:
        raise AssertionError("an over-cap request was accepted")
    assert session.calls == []


def test_st_and_batch_combination_at_the_call_cap_is_accepted():
    session = Session()
    execute(
        session=session,
        config=Config(),
        target_key="longhu_history",
        path=None,
        params={"a": "DailyLimitPerformance", "c": "HisHomeDingPan", "st": 3000},
    )
    assert len(session.calls) == MAX_EXPECTED_CALLS == 10


def test_cross_product_of_st_and_batch_values_is_also_capped():
    session = Session()
    values = [f"{index:06d}" for index in range(301)]  # 2 batch chunks
    try:
        execute(
            session=session,
            config=Config(),
            target_key="longhu_quote",
            path=None,
            params={"a": "GetStockPanKou", "c": "StockL2Data", "st": 1501},  # 6 page chunks * 2 = 12
            batch_param="StockIDs",
            batch_values=values,
        )
    except ValueError as error:
        assert "upstream calls" in str(error)
    else:
        raise AssertionError("a cross-product over-cap request was accepted")
    assert session.calls == []


def test_timeout_is_capped_regardless_of_configured_value():
    session = Session()

    @dataclass
    class SlowConfig(Config):
        timeout_seconds: float = 600.0

    execute(
        session=session, config=SlowConfig(), target_key="longhu_quote", path=None,
        params={"a": "GetStockPanKou", "c": "StockL2Data"},
    )
    assert session.calls[0]["timeout"] == MAX_TIMEOUT_SECONDS


def test_upstream_http_failure_is_sanitized():
    session = Session()
    session.get = lambda *args, **kwargs: FailingResponse()
    try:
        execute(
            session=session,
            config=Config(),
            target_key="longhu_quote",
            path=None,
            params={"a": "UnknownAction", "c": "UnknownController"},
        )
    except UpstreamStockApiError as error:
        assert str(error) == "stock-data upstream rejected the request (HTTP 404)"
        assert "credential-bearing" not in str(error)
    else:
        raise AssertionError("upstream failure was not raised")
