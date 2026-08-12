"""Pydantic request contracts shared by HTTP routes and service functions."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .capability_registry import api_capability
from .tushare_catalog import TUSHARE_CATALOG
from .tushare_official import AUDIT_FOCUS_APIS, official_spec
from .tushare_providers import ProviderPreference


class DailyBar(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    trading_date: date
    close: Decimal = Field(gt=0)
    open: Decimal | None = Field(default=None, ge=0)
    high: Decimal | None = Field(default=None, ge=0)
    low: Decimal | None = Field(default=None, ge=0)
    pre_close: Decimal | None = Field(default=None, ge=0)
    volume: Decimal | None = Field(default=None, ge=0)
    amount: Decimal | None = Field(default=None, ge=0)
    adj_factor: Decimal | None = Field(default=None, gt=0)
    is_suspended: bool | None = None
    limit_up: Decimal | None = Field(default=None, gt=0)
    limit_down: Decimal | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    is_st: bool | None = None
    source: str = Field(default="manual", max_length=60)
    available_at: datetime | None = None

    @model_validator(mode="after")
    def validate_ohlc(self) -> "DailyBar":
        prices = [value for value in (self.open, self.high, self.low, self.close) if value is not None]
        if self.high is not None and self.high < max(prices):
            raise ValueError("high must be at least open, low and close")
        if self.low is not None and self.low > min(prices):
            raise ValueError("low must be at most open, high and close")
        return self


class BarsImport(BaseModel):
    bars: list[DailyBar] = Field(min_length=1, max_length=10000)


class TushareSyncRequest(BaseModel):
    trade_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    symbols: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_date_range(self) -> "TushareSyncRequest":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be supplied together")
        if self.trade_date is not None and self.start_date is not None:
            raise ValueError("use trade_date or start_date/end_date, not both")
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not be before start_date")
            if (self.end_date - self.start_date).days > 45:
                raise ValueError("range is capped at 45 days; historical bulk import must use offline files")
        return self


AUDITED_SUPER_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "index_daily": ("ts_code",), "moneyflow_cnt_ths": ("trade_date",),
    "ths_member": ("ts_code",), "top_list": ("trade_date",),
    "fut_basic": ("exchange",), "cn_gdp": ("q",),
}


class TushareFetchRequest(BaseModel):
    api_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    provider: ProviderPreference = "auto"
    params: dict[str, Any] = Field(default_factory=dict)
    fields: str | None = Field(default=None, max_length=2000)
    max_rows: int = Field(default=500, ge=1, le=10_000)
    paginate: bool = False
    page_size: int = Field(default=1000, ge=100, le=3000)
    max_pages: int = Field(default=10, ge=1, le=20)
    require_complete: bool = False
    force_refresh: bool = False

    @model_validator(mode="after")
    def validate_safe_request(self) -> "TushareFetchRequest":
        if self.api_name not in TUSHARE_CATALOG:
            raise ValueError("api_name is not in the enabled Tushare-compatible catalog")
        contract = api_capability(self.api_name)
        if contract.status in {"offline_only", "unsupported"}:
            raise ValueError(f"{self.api_name} is currently unavailable: {contract.note}")
        if self.require_complete and not self.paginate:
            raise ValueError("require_complete requires paginate=true so the terminal page can be verified")
        if self.api_name == "ths_member" and self.require_complete and self.provider == "super_get":
            raise ValueError("complete ths_member snapshots require provider=super, super_sdk, or auto; the GET route is bounded-only")
        if self.paginate and self.max_rows < self.page_size:
            raise ValueError("max_rows must be at least page_size for paginated requests")
        start, end = self.params.get("start_date"), self.params.get("end_date")
        if (start is None) != (end is None):
            raise ValueError("start_date and end_date must be supplied together")
        if start and end:
            try:
                start_date = datetime.strptime(str(start), "%Y%m%d").date()
                end_date = datetime.strptime(str(end), "%Y%m%d").date()
            except ValueError as error:
                raise ValueError("date parameters must use YYYYMMDD") from error
            maximum_days = 370 if self.api_name == "trade_cal" else 45
            if end_date < start_date or (end_date - start_date).days > maximum_days:
                raise ValueError(f"online range is capped at {maximum_days} days; use offline files for historical bulk import")
        if self.api_name in {"daily", "weekly", "monthly", "adj_factor", "daily_basic", "stk_limit", "suspend_d", "moneyflow", "cyq_perf", "cyq_chips", "stk_factor", "stk_factor_pro"} and not self.params.get("ts_code"):
            raise ValueError(f"{self.api_name} requires an explicit ts_code")
        required_params = set((official_spec(self.api_name).required_params if official_spec(self.api_name) else ()))
        required_params.update(AUDITED_SUPER_REQUIRED_PARAMS.get(self.api_name, ()))
        for required in required_params:
            if self.params.get(required) in (None, ""):
                raise ValueError(f"{self.api_name} requires an explicit {required}")
        for date_key in ("trade_date", "cal_date", "date"):
            if self.params.get(date_key) not in (None, "") and not re.fullmatch(r"\d{8}", str(self.params[date_key])):
                raise ValueError(f"{date_key} must use YYYYMMDD")
        if self.params.get("q") not in (None, "") and not re.fullmatch(r"\d{4}Q[1-4]", str(self.params["q"])):
            raise ValueError("q must use YYYYQn form, for example 2026Q3")
        if self.api_name in {"rt_min", "rt_min_daily"} and self.params.get("freq") not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
            raise ValueError("freq must be one of 1MIN, 5MIN, 15MIN, 30MIN, 60MIN")
        if self.api_name == "ths_member" and not re.fullmatch(r"\d{6}\.TI", str(self.params.get("ts_code") or "")):
            raise ValueError("ths_member ts_code must use the exact THS board code, for example 885573.TI")
        if self.api_name == "fut_basic" and self.params.get("exchange") not in {"CFFEX", "DCE", "CZCE", "SHFE", "INE", "GFEX"}:
            raise ValueError("fut_basic exchange is not a supported futures exchange")
        return self


class IntradaySectorReportRequest(BaseModel):
    kind: Literal["all", "concept", "industry"] = "all"
    top_stocks: int = Field(default=10, ge=1, le=10)
    hydrate_top_boards: int = Field(default=0, ge=0, le=5)


class StrategyDecisionRequest(BaseModel):
    """Create an evidence-preserving market decision snapshot."""

    session: Literal["intraday", "close"] = "intraday"
    kind: Literal["all", "concept", "industry"] = "all"
    limit: int = Field(default=20, ge=1, le=50)
    validate_tushare_realtime: bool = True


class StrategyReviewRequest(BaseModel):
    """Materialize a review only from evidence already persisted locally."""

    session: Literal["midday", "close"]
    as_of_date: date | None = None
    persist: bool = True


class PostCloseStrategyRequest(BaseModel):
    as_of_date: date | None = None
    limit: int = Field(default=20, ge=1, le=100)
    minimum_full_market_symbols: int = Field(default=1000, ge=100, le=10000)


class StrategyPatternMiningRequest(BaseModel):
    """Replay a bounded, stratified limit-up sample using saved minute evidence."""

    as_of_date: date | None = None
    max_symbols: int = Field(default=20, ge=4, le=20)
    per_cohort: int = Field(default=6, ge=1, le=6)
    focus_symbols: list[str] = Field(default_factory=list, max_length=10)
    refresh_limit_sources: bool = True

    @model_validator(mode="after")
    def validate_focus_symbols(self) -> "StrategyPatternMiningRequest":
        self.focus_symbols = list(dict.fromkeys(str(symbol).strip().upper() for symbol in self.focus_symbols if str(symbol).strip()))
        invalid = [symbol for symbol in self.focus_symbols if not re.fullmatch(r"\d{6}\.(SZ|SH|BJ)", symbol)]
        if invalid:
            raise ValueError(f"focus_symbols are invalid: {', '.join(invalid)}")
        return self


class IntradayWatchlistRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    label: str | None = Field(default=None, max_length=120)
    enabled: bool = True
    alert_on_entry: bool = True
    alert_on_exit: bool = True
    entry_price: Decimal | None = Field(default=None, gt=0)
    available_quantity: int = Field(default=0, ge=0)
    hard_stop: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IntradayScanRequest(BaseModel):
    """Run a bounded, non-executable scan over explicitly watched symbols."""

    # Tencent's all-A snapshot is fetched once per scan, so allowing the
    # explicit research pool to exceed the separate 20-symbol depth stream
    # does not multiply external traffic.  The depth stream still advertises
    # and enforces its own strict 20-symbol bound.
    symbols: list[str] = Field(default_factory=list, max_length=40)
    realtime_validation_limit: int = Field(default=4, ge=0, le=4)
    realtime_validation_offset: int = Field(default=0, ge=0, le=40)

    @model_validator(mode="after")
    def normalize_symbols(self) -> "IntradayScanRequest":
        normalized = []
        for symbol in self.symbols:
            value = symbol.upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
                raise ValueError("symbols must use the Tushare form, for example 600176.SH")
            if value not in normalized:
                normalized.append(value)
        self.symbols = normalized
        return self


class MinuteSessionCaptureRequest(BaseModel):
    """Capture explicit-watch end-of-session minute evidence for baseline building."""

    symbols: list[str] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def normalize_symbols(self) -> "MinuteSessionCaptureRequest":
        normalized = []
        for symbol in self.symbols:
            value = symbol.upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
                raise ValueError("symbols must use the Tushare form, for example 600176.SH")
            if value not in normalized:
                normalized.append(value)
        self.symbols = normalized
        return self


class OfflineMinuteImportRequest(BaseModel):
    """Reference a bounded local CSV; remote/history URLs are intentionally absent."""

    file_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.csv$")
    source_name: str = Field(default="offline-provider", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
    max_rows: int = Field(default=5_000_000, ge=1, le=5_000_000)


class GenerateRequest(BaseModel):
    as_of_date: date | None = None
    lookback_days: int = Field(default=21, ge=1, le=180)
    limit: int = Field(default=20, ge=1, le=100)
    universe_key: str = Field(default="core", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    horizon_days: Literal[1, 5, 20, 60] = 20


class StockStudyRequest(BaseModel):
    as_of_date: date | None = None
    lookback_days: int = Field(default=21, ge=5, le=45)


class AkShareProbeRequest(BaseModel):
    symbol: str = Field(default="000636.SZ", pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    trade_date: date | None = None
    lookback_days: int = Field(default=21, ge=1, le=45)
    include_market_summary: bool = True
    include_lhb: bool = True
    include_strong_pool: bool = True
    include_supplements: bool = True
    include_board_taxonomy: bool = True
    include_moneyflow: bool = True
    include_limit_pools: bool = True
    include_lhb_supplements: bool = True
    include_block_trades: bool = True
    include_corporate_risk: bool = True
    include_analyst_heat: bool = True
    include_index_fund: bool = True
    include_macro_cross_asset: bool = False
    board_limit: int = Field(default=3, ge=0, le=30)


class UniverseUpdateRequest(BaseModel):
    universe_key: str = Field(default="core", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    symbols: list[str] = Field(min_length=1, max_length=2000)
    enabled: bool = True
    priority: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def normalize_symbols(self) -> "UniverseUpdateRequest":
        values = sorted({item.strip().upper() for item in self.symbols if item and item.strip()})
        if not values or any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value) for value in values):
            raise ValueError("symbols must use Tushare form, for example 000636.SZ")
        self.symbols = values
        return self


class ClaimReviewRequest(BaseModel):
    status: Literal["approved", "rejected"]
    symbol: str | None = Field(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    reviewer_note: str | None = Field(default=None, max_length=1000)


class FactorEvaluationRequest(BaseModel):
    universe_key: str = Field(default="core", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    factor_keys: list[str] = Field(default_factory=list, max_length=32)
    start_date: date | None = None
    end_date: date | None = None
    horizon_days: Literal[1, 5, 20, 60] = 5


class StrategyBacktestRequest(BaseModel):
    strategy_key: Literal["multi_factor_rank_v1"] = "multi_factor_rank_v1"
    universe_key: str = Field(default="core", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    start_date: date | None = None
    end_date: date | None = None
    rebalance_days: int = Field(default=5, ge=1, le=60)
    hold_days: int = Field(default=5, ge=1, le=60)
    top_n: int = Field(default=20, ge=1, le=500)
    total_cost_bps: float = Field(default=18.0, ge=0, le=500)
    factors: list[str] = Field(default_factory=lambda: ["momentum_20d", "sma_gap_20d", "volume_ratio_20d", "reversal_5d"], min_length=1, max_length=16)


class RemoteReportImport(BaseModel):
    report: dict[str, Any]


class RemoteReportReprocessRequest(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class SnapshotRequest(BaseModel):
    as_of_date: date | None = None
    knowledge_cutoff: datetime | None = None


class HistoricalCoverageEstimateRequest(BaseModel):
    years: int = Field(default=3, ge=1, le=10)
    include_minute: bool = False
    universe_symbols: int | None = Field(default=None, ge=1, le=20000)
    trading_days_per_year: int = Field(default=244, ge=200, le=260)


class FetchRunReconcileRequest(BaseModel):
    max_age_minutes: int = Field(default=90, ge=10, le=1440)
    dry_run: bool = False
    terminal_status: Literal["failed", "blocked"] = "failed"


class MarketUniverseSyncRequest(BaseModel):
    universe_key: str = Field(default="all_a", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    provider: ProviderPreference = "auto"
    minimum_rows: int = Field(default=5000, ge=100, le=10000)


class FullMarketDailySyncRequest(BaseModel):
    trade_date: date | None = None
    provider: ProviderPreference = "auto"
    minimum_rows: int = Field(default=5000, ge=100, le=10000)


class MarketSnapshotRequest(BaseModel):
    session: Literal["midday", "close"]
    universe_key: str = Field(default="all_a", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    refresh_public_quotes: bool = True


class AnnouncementSyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=50)
    universe_key: str = Field(default="core", pattern=r"^[a-z][a-z0-9_-]{0,48}$")
    start_date: date | None = None
    end_date: date | None = None
    lookback_days: int = Field(default=45, ge=1, le=90)
    max_pages_per_symbol: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def validate_symbol_scope(self) -> "AnnouncementSyncRequest":
        self.symbols = sorted({item.strip().upper() for item in self.symbols if item and item.strip()})
        invalid = [item for item in self.symbols if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item)]
        if invalid:
            raise ValueError("symbols must use Tushare form, for example 000636.SZ")
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("start_date and end_date must be supplied together")
        if self.start_date and self.end_date:
            if self.end_date < self.start_date:
                raise ValueError("end_date must not be before start_date")
            if (self.end_date - self.start_date).days > 90:
                raise ValueError("announcement range is capped at 90 days")
        return self


class RealtimeProbeRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["000636.SZ"], min_length=1, max_length=3)
    frequency: Literal["1MIN", "5MIN", "15MIN", "30MIN", "60MIN"] = "1MIN"
    etf_symbol: str = Field(default="159919.SZ", pattern=r"^\d{6}\.(SH|SZ)$")
    index_symbol: str = Field(default="000300.SH", pattern=r"^\d{6}\.(SH|SZ)$")
    sw_symbol: str = Field(default="801080.SI", pattern=r"^\d{6}\.(SI|TI)$")
    futures_symbol: str | None = Field(default=None, pattern=r"^[A-Z]{1,3}\d{4}\.[A-Z]+$")

    @model_validator(mode="after")
    def validate_symbols(self) -> "RealtimeProbeRequest":
        normalized = []
        for symbol in self.symbols:
            value = str(symbol).upper()
            if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", value):
                raise ValueError("symbols must use Tushare codes, for example 000636.SZ")
            if value not in normalized:
                normalized.append(value)
        self.symbols = normalized
        return self


class TushareCapabilityAuditRequest(BaseModel):
    api_names: list[str] = Field(default_factory=lambda: list(AUDIT_FOCUS_APIS[:8]), min_length=1, max_length=12)
    providers: list[Literal["primary", "super", "super_sdk", "super_get"]] = Field(default_factory=lambda: ["primary", "super"], min_length=1, max_length=4)
    symbol: str = Field(default="000636.SZ", pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    as_of_date: date | None = None
    max_rows: int = Field(default=10, ge=1, le=50)

    @model_validator(mode="after")
    def validate_audit_scope(self) -> "TushareCapabilityAuditRequest":
        self.api_names = list(dict.fromkeys(self.api_names))
        self.providers = list(dict.fromkeys(self.providers))
        unknown = [api_name for api_name in self.api_names if api_name not in TUSHARE_CATALOG]
        if unknown:
            raise ValueError(f"api_names are not enabled: {', '.join(unknown)}")
        return self


class SectorCatalogSyncRequest(BaseModel):
    index_type: Literal["N", "I", "R", "S", "ST", "BB"] = "N"
    all_types: bool = False
    sync_members: bool = False
    member_offset: int = Field(default=0, ge=0, le=10000)
    member_limit: int = Field(default=0, ge=0, le=50)
    resume: bool = False

    @model_validator(mode="after")
    def validate_member_request(self) -> "SectorCatalogSyncRequest":
        if self.sync_members and self.member_limit < 1:
            raise ValueError("member_limit must be at least 1 when sync_members is true")
        if not self.sync_members and self.member_limit:
            raise ValueError("member_limit requires sync_members=true")
        if self.all_types and self.sync_members:
            raise ValueError("all_types only refreshes directories; synchronize members in explicit bounded batches")
        if self.resume and (not self.sync_members or self.member_offset):
            raise ValueError("resume=true requires sync_members=true and member_offset=0")
        return self


class SectorFlowSyncRequest(BaseModel):
    trade_date: date | None = None
    provider: ProviderPreference = "auto"


class ConceptCandidateSyncRequest(SectorFlowSyncRequest):
    top_concepts: int = Field(default=8, ge=1, le=12)
    leaders_per_concept: int = Field(default=3, ge=1, le=5)


class ConceptMemberSyncRequest(SectorFlowSyncRequest):
    member_offset: int = Field(default=0, ge=0, le=10_000)
    member_limit: int = Field(default=25, ge=1, le=50)
    refresh_flow_catalog: bool = False
    resume: bool = False

    @model_validator(mode="after")
    def validate_resume_request(self) -> "ConceptMemberSyncRequest":
        if self.resume and self.member_offset:
            raise ValueError("resume=true selects the next incomplete concepts and cannot use member_offset")
        return self


class ConceptMemberBackfillRequest(SectorFlowSyncRequest):
    batch_size: int = Field(default=25, ge=1, le=25)
    refresh_flow_catalog: bool = False


class EastmoneyBoardMemberSyncRequest(BaseModel):
    kind: Literal["concept", "industry"] = "concept"
    member_offset: int = Field(default=0, ge=0, le=10_000)
    member_limit: int = Field(default=25, ge=1, le=50)
    resume: bool = False

    @model_validator(mode="after")
    def validate_resume_request(self) -> "EastmoneyBoardMemberSyncRequest":
        if self.resume and self.member_offset:
            raise ValueError("resume=true selects stale or failed boards and cannot use member_offset")
        return self


class AllBoardMemberBackfillRequest(BaseModel):
    batch_size: int = Field(default=10, ge=1, le=25)
    include_ths: bool = True
    include_eastmoney: bool = True
    refresh_catalogs: bool = False

    @model_validator(mode="after")
    def validate_sources(self) -> "AllBoardMemberBackfillRequest":
        if not self.include_ths and not self.include_eastmoney:
            raise ValueError("at least one board-member source must be selected")
        return self


class PostCloseRefreshRequest(BaseModel):
    trade_date: date | None = None
    include_macro_cross_asset: bool = True
    include_announcements: bool = True
    announcement_limit: int = Field(default=20, ge=0, le=50)


class BoardResearchRunRequest(ConceptCandidateSyncRequest):
    study_lookback_days: int = Field(default=21, ge=5, le=45)
    max_stock_studies: int = Field(default=6, ge=1, le=12)
    sync_announcements: bool = True
