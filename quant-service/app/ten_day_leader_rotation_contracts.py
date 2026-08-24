"""HTTP contracts for ten-day leader-rotation research."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TenDayLeaderRotationRunRequest(BaseModel):
    as_of_date: date | None = None
    per_board_limit: int = Field(default=30, ge=1, le=30)
    minimum_full_market_symbols: int = Field(default=5000, ge=1000, le=10000)


class _ResearchResponse(BaseModel):
    """Keep read additions forward-compatible without hiding typed core fields."""

    model_config = ConfigDict(extra="allow")


class TenDayLeaderRotationRunResponse(_ResearchResponse):
    run_id: UUID | None = None
    run_key: str | None = None
    as_of_date: date | None = None
    strategy_available_at: datetime | None = None
    model_version: str | None = None
    status: str | None = None
    source_status: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenDayLeaderRotationCandidateResponse(_ResearchResponse):
    board: str | None = None
    board_rank: int | None = None
    symbol: str | None = None
    name: str | None = None
    ten_day_return_pct: float | None = None
    current_return_pct: float | None = None
    candidate_path: str | None = None
    shadow_state: str | None = None
    shadow_eligible: bool | None = None
    decision_eligible: bool | None = None
    evidence: dict[str, Any] | None = None
    reason_codes: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    source_snapshot: dict[str, Any] | None = None
    discovered_at: datetime | None = None
    intraday_observed_at: datetime | None = None
    intraday_quote_source: str | None = None
    intraday_shadow_state: str | None = None
    intraday_shadow_eligible: bool | None = None
    intraday_evidence: dict[str, Any] | None = None
    intraday_reason_codes: list[str] | None = None
    intraday_risk_flags: list[str] | None = None


class TenDayLeaderRotationIntradayBatchResponse(BaseModel):
    scan_id: UUID
    observed_at: datetime
    observed_count: int = Field(ge=0)
    shadow_eligible_count: int = Field(ge=0)
    decision_eligible_count: Literal[0]
    quote_sources: list[str] = Field(min_length=1)


class TenDayLeaderRotationIntradayResponse(BaseModel):
    pool_run: TenDayLeaderRotationRunResponse | None = None
    latest_batch: TenDayLeaderRotationIntradayBatchResponse | None = None


class TenDayLeaderRotationLatestResponse(BaseModel):
    run: TenDayLeaderRotationRunResponse | None = None
    candidates: list[TenDayLeaderRotationCandidateResponse] = Field(default_factory=list)
    intraday: TenDayLeaderRotationIntradayResponse = Field(default_factory=TenDayLeaderRotationIntradayResponse)
    scope: Literal["research_only_no_orders"]
    notice: str


__all__ = [
    "TenDayLeaderRotationRunRequest",
    "TenDayLeaderRotationLatestResponse",
]
