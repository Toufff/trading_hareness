"""Full authenticated proxy for the owner's documented stock-data endpoints.

The peer receives every documented upstream capability without receiving the
vendor token or device identity.  Only transport hosts are allow-listed to
prevent SSRF.  Operation names and query parameters are otherwise passed
through.  Vendor page sizes are physically capped at 300 and larger logical
requests are split automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from math import ceil
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin

import requests

MAX_PHYSICAL_BATCH = 300
SENSITIVE_QUERY_KEYS = frozenset({"token", "userid", "deviceid"})


class UpstreamStockApiError(RuntimeError):
    """Sanitized upstream failure that never embeds credential-bearing URLs."""

    def __init__(self, kind: str, status_code: int | None = None) -> None:
        self.kind = kind
        self.status_code = status_code
        suffix = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"stock-data upstream {kind}{suffix}")


@dataclass(frozen=True)
class Target:
    key: str
    base_url: str
    default_path: str
    credentials: str
    description: str


TARGETS: dict[str, Target] = {
    "longhu_history": Target(
        "longhu_history", "https://apphis.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu historical interfaces",
    ),
    "longhu_quote": Target(
        "longhu_quote", "https://apphwhq.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu quote, minute, auction and stock L2 interfaces",
    ),
    "longhu_market": Target(
        "longhu_market", "https://apphq.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu market monitor, ranking and global-index interfaces",
    ),
    "longhu_market_wide": Target(
        "longhu_market_wide", "https://apphwshhq.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu market breadth, plate and research interfaces",
    ),
    "longhu_lhb": Target(
        "longhu_lhb", "https://applhb.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu shareholder, institution and topic interfaces",
    ),
    "longhu_article": Target(
        "longhu_article", "https://apparticle.longhuvip.com", "/w1/api/index.php",
        "default", "Longhu news and flash interfaces",
    ),
    "xuangubao": Target(
        "xuangubao", "https://flash-api.xuangubao.com.cn", "/api/pool/detail",
        "none", "Xuangubao public pool, indicator and surge interfaces",
    ),
    "fupanwang": Target(
        "fupanwang", "https://api.fupanwang.com", "/kpl/zhibo",
        "none", "Fupanwang intraday live feed",
    ),
}

DOCUMENTED_OPERATIONS: tuple[dict[str, str], ...] = (
    {"target": "longhu_history", "action": "GetKLineDay_W14", "controller": "StockLineData"},
    {"target": "longhu_quote", "action": "GetStockTrendIncremental", "controller": "StockL2Data"},
    {"target": "longhu_quote", "action": "GetStockPanKou", "controller": "StockL2Data"},
    {"target": "longhu_market", "action": "GetMainMonitor_w30", "controller": "StockYiDongKanPan"},
    {"target": "longhu_market", "action": "GetWeiTuo_W14", "controller": "StockL2Data"},
    {"target": "longhu_market_wide", "action": "GetPlateInfo_w38", "controller": "DailyLimitResumption"},
    {"target": "longhu_market_wide", "action": "RiseFallAnalysis", "controller": "HomeDingPan"},
    {"target": "longhu_history", "action": "RiseFallAnalysis", "controller": "HisHomeDingPan"},
    {"target": "longhu_market_wide", "action": "MoodNumCount", "controller": "MarketMood"},
    {"target": "longhu_market_wide", "action": "GetPlate_Info_QJ", "controller": "ZhiShuRanking"},
    {"target": "longhu_market", "action": "ChangeStatistics", "controller": "HomeDingPan"},
    {"target": "longhu_history", "action": "ChangeStatistics", "controller": "HisHomeDingPan"},
    {"target": "longhu_quote", "action": "MorningBiddingList", "controller": "HomeDingPan"},
    {"target": "longhu_history", "action": "MorningBiddingList", "controller": "HisHomeDingPan"},
    {"target": "longhu_quote", "action": "GetStockBid", "controller": "StockL2Data"},
    {"target": "longhu_history", "action": "ZhiBoContent", "controller": "HisConceptionPoint"},
    {"target": "longhu_market", "action": "RealRankingInfo", "controller": "ZhiShuRanking"},
    {"target": "longhu_history", "action": "RealRankingInfo", "controller": "ZhiShuRanking"},
    {"target": "longhu_market_wide", "action": "ZhiShuStockList_W8", "controller": "ZhiShuRanking"},
    {"target": "longhu_quote", "action": "DailyLimitPerformance", "controller": "HomeDingPan"},
    {"target": "longhu_history", "action": "DailyLimitPerformance", "controller": "HisHomeDingPan"},
    {"target": "longhu_quote", "action": "DailyLimitPerformance2", "controller": "HomeDingPan"},
    {"target": "longhu_history", "action": "DailyLimitPerformance2", "controller": "HisHomeDingPan"},
    {"target": "longhu_market_wide", "action": "GroupCount_w28", "controller": "StockNewHigh"},
    {"target": "longhu_market", "action": "Radar", "controller": "HomeDingPan"},
    {"target": "longhu_market", "action": "GetHotPHB", "controller": "StockBidYiDong"},
    {"target": "longhu_market", "action": "GlobalCommon", "controller": "GlobalIndex"},
    {"target": "longhu_quote", "action": "StockChouMaByTimeNew_W5", "controller": "StockYiDongKanPan"},
    {"target": "longhu_history", "action": "GetStockChouMa_New", "controller": "StockL2History"},
    {"target": "longhu_quote", "action": "GetStockDaDanTrendIncremental", "controller": "StockL2Data"},
    {"target": "longhu_quote", "action": "GetZhangTingGene", "controller": "StockL2Data"},
    {"target": "longhu_history", "action": "GetPMSL_KQXY", "controller": "FuPanLa"},
    {"target": "longhu_quote", "action": "GetBKJJ_W36", "controller": "StockBidYiDong"},
    {"target": "longhu_quote", "action": "GetBKJJBL", "controller": "StockBidYiDong"},
    {"target": "longhu_market_wide", "action": "GetInterviewsByDateZS", "controller": "StockLineData"},
    {"target": "longhu_market_wide", "action": "GetInterviewsByDateStock", "controller": "StockLineData"},
    {"target": "longhu_history", "action": "GGList_JGCC", "controller": "ZhuLiChiCang"},
    {"target": "longhu_history", "action": "GGList_JGCC_Plate_Stocks", "controller": "ZhuLiChiCang"},
    {"target": "longhu_history", "action": "GGList_BXZJ", "controller": "ZhuLiChiCang"},
    {"target": "longhu_history", "action": "GGList_BXZJ_Stocks", "controller": "ZhuLiChiCang"},
    {"target": "longhu_market_wide", "action": "GetPianLiZhi_Index", "controller": "StockBidYiDong"},
    {"target": "longhu_lhb", "action": "GuDongRenShu", "controller": "YiDianCangWei"},
    {"target": "longhu_lhb", "action": "JGStockListox", "controller": "JGTracking"},
    {"target": "longhu_lhb", "action": "GetJGNameID", "controller": "JGTracking"},
    {"target": "longhu_market_wide", "action": "GetStockIDPlate", "controller": "StockL2Data"},
    {"target": "longhu_article", "action": "GetTopList", "controller": "PCNewsFlash"},
    {"target": "longhu_article", "action": "GetList", "controller": "PCNewsFlash"},
    {"target": "longhu_lhb", "action": "InfoList", "controller": "Topic"},
    {"target": "longhu_lhb", "action": "InfoZS", "controller": "Topic"},
    {"target": "longhu_lhb", "action": "InfoGet", "controller": "Topic"},
)


def documented_examples() -> list[dict[str, Any]]:
    path = Path(__file__).with_name("licensed_stock_api_examples.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("licensed stock API examples must be a list")
    return [row for row in payload if isinstance(row, dict)]


def catalog() -> dict[str, Any]:
    return {
        "targets": [
            {
                "key": target.key,
                "base_url": target.base_url,
                "default_path": target.default_path,
                "credentials": target.credentials,
                "description": target.description,
            }
            for target in TARGETS.values()
        ],
        "documented_operations": list(DOCUMENTED_OPERATIONS),
        "documented_examples": documented_examples(),
        "external_paths": {
            "xuangubao": [
                "/api/pool/detail",
                "/api/market_indicator/line",
                "/api/surge_stock/stocks",
                "/api/surge_stock/plates",
            ],
            "fupanwang": ["/kpl/zhibo"],
        },
        "physical_batch_limit": MAX_PHYSICAL_BATCH,
        "operation_restriction": "none_within_registered_targets",
    }


def _path(target: Target, requested: str | None) -> str:
    value = (requested or target.default_path).strip()
    if not value.startswith("/") or value.startswith("//") or ".." in value.split("/"):
        raise ValueError("path must be an absolute path without traversal")
    return value


def _integer(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in params.items()
        if str(key).lower() not in SENSITIVE_QUERY_KEYS and value is not None
    }


def _offset_key(params: Mapping[str, Any]) -> str:
    return "index" if "index" in params and "Index" not in params else "Index"


def _credential_params(config: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    action = str(params.get("a") or "")
    device_id = (
        getattr(config, "ranking_device_id", "")
        if action == "RealRankingInfo"
        else getattr(config, "device_id", "")
    )
    return {
        "PhoneOSNew": 1,
        "DeviceID": device_id,
        "VerSion": getattr(config, "version", ""),
        "Token": getattr(config, "token", ""),
        "UserID": getattr(config, "user_id", ""),
    }


def _request_json(
    session: requests.Session,
    *,
    url: str,
    params: Mapping[str, Any],
    timeout_seconds: float,
    retries: int,
) -> Any:
    last_error: UpstreamStockApiError | None = None
    for attempt in range(max(1, retries)):
        try:
            response = session.get(url, params=dict(params), timeout=timeout_seconds)
            response.raise_for_status()
            try:
                return response.json()
            except (ValueError, requests.exceptions.JSONDecodeError):
                last_error = UpstreamStockApiError("returned non-JSON content", response.status_code)
        except requests.Timeout:
            last_error = UpstreamStockApiError("timed out")
        except requests.HTTPError as error:
            status_code = error.response.status_code if error.response is not None else None
            last_error = UpstreamStockApiError("rejected the request", status_code)
        except requests.RequestException:
            last_error = UpstreamStockApiError("network failure")
        if attempt + 1 < max(1, retries):
            import time
            time.sleep(0.35 * (attempt + 1))
    assert last_error is not None
    raise last_error


def _physical_requests(
    params: Mapping[str, Any],
    *,
    batch_param: str | None,
    batch_values: Sequence[Any],
    batch_separator: str,
) -> list[tuple[int | None, int | None, int | None, dict[str, Any]]]:
    cleaned = dict(params)
    requested_size = _integer(cleaned.get("st"))
    base_offset_key = _offset_key(cleaned)
    base_offset = _integer(cleaned.get(base_offset_key))
    page_sizes = (
        [
            min(MAX_PHYSICAL_BATCH, requested_size - offset)
            for offset in range(0, requested_size, MAX_PHYSICAL_BATCH)
        ]
        if requested_size > MAX_PHYSICAL_BATCH
        else [requested_size if requested_size > 0 else None]
    )
    value_batches: list[list[Any] | None] = (
        [
            list(batch_values[index:index + MAX_PHYSICAL_BATCH])
            for index in range(0, len(batch_values), MAX_PHYSICAL_BATCH)
        ]
        if batch_param and batch_values
        else [None]
    )

    result: list[tuple[int | None, int | None, int | None, dict[str, Any]]] = []
    consumed = 0
    for size in page_sizes:
        for values in value_batches:
            current = dict(cleaned)
            offset: int | None = None
            if size is not None:
                current["st"] = size
                if requested_size > MAX_PHYSICAL_BATCH:
                    offset = base_offset + consumed
                    current[base_offset_key] = offset
            if values is not None and batch_param:
                current[batch_param] = batch_separator.join(str(value) for value in values)
            result.append((offset, size, len(values) if values is not None else None, current))
        if size is not None:
            consumed += size
    return result


def execute(
    *,
    session: requests.Session,
    config: Any,
    target_key: str,
    path: str | None,
    params: Mapping[str, Any],
    batch_param: str | None = None,
    batch_values: Sequence[Any] = (),
    batch_separator: str = ",",
) -> dict[str, Any]:
    target = TARGETS.get(target_key)
    if target is None:
        raise ValueError(f"unknown stock API target: {target_key}")
    resolved_path = _path(target, path)
    sanitized = _clean_params(params)
    requests_to_make = _physical_requests(
        sanitized,
        batch_param=batch_param,
        batch_values=batch_values,
        batch_separator=batch_separator,
    )
    pages: list[dict[str, Any]] = []
    url = urljoin(target.base_url, resolved_path)

    for offset, size, batch_count, physical_params in requests_to_make:
        request_params = dict(physical_params)
        if target.credentials != "none":
            request_params = {**_credential_params(config, physical_params), **physical_params}
        payload = _request_json(
            session,
            url=url,
            params=request_params,
            timeout_seconds=float(getattr(config, "timeout_seconds", 20.0)),
            retries=int(getattr(config, "retries", 3)),
        )
        pages.append({
            "offset": offset,
            "size": size,
            "batch_count": batch_count,
            "payload": payload,
        })

    requested_size = _integer(sanitized.get("st"))
    return {
        "target": target.key,
        "path": resolved_path,
        "calls": len(pages),
        "batched": len(pages) > 1,
        "physical_batch_limit": MAX_PHYSICAL_BATCH,
        "requested_size": requested_size if requested_size > 0 else None,
        "batch_param": batch_param,
        "batch_value_count": len(batch_values) if batch_param else None,
        "pages": pages,
    }


def expected_call_count(
    *,
    requested_size: int | None = None,
    batch_value_count: int | None = None,
) -> int:
    page_calls = ceil(requested_size / MAX_PHYSICAL_BATCH) if requested_size else 1
    value_calls = ceil(batch_value_count / MAX_PHYSICAL_BATCH) if batch_value_count else 1
    return page_calls * value_calls


__all__ = [
    "DOCUMENTED_OPERATIONS",
    "MAX_PHYSICAL_BATCH",
    "TARGETS",
    "UpstreamStockApiError",
    "catalog",
    "documented_examples",
    "execute",
    "expected_call_count",
]
