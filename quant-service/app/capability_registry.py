"""Explicit provider capability contracts for the enabled market APIs.

The catalog is a customer-facing allow-list.  This registry adds the
operational facts that decide whether an API may participate in research:
frequency, preferred source order, and whether it is currently safe to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .tushare_official import HISTORICAL_MINUTE_APIS, OFFICIAL_EXTENSIONS, REALTIME_MARKET_HOURS_APIS, official_spec


@dataclass(frozen=True)
class ApiCapability:
    frequency: str
    decision_eligible: bool
    preferred_providers: tuple[str, ...]
    status: str = "declared"
    note: str = "Supplier-declared capability; retain raw evidence until validated."


# These specialty endpoints were verified against the super gateway.  It is
# therefore the first candidate, while the standard source remains a fallback
# when its contract permits the same request.
SUPER_PREFERRED_APIS: Final[frozenset[str]] = frozenset({
    "cyq_perf", "cyq_chips", "moneyflow", "moneyflow_ths", "moneyflow_dc",
    "moneyflow_ind_ths", "moneyflow_ind_dc", "moneyflow_mkt_dc", "moneyflow_hsgt", "moneyflow_cnt_ths",
    "stk_factor_pro", "report_rc", "top_list", "top_inst", "hm_list", "hm_detail",
    "limit_list_ths", "limit_list_d", "limit_step", "limit_cpt_list", "ths_hot",
    "dc_hot", "ths_index", "ths_daily", "ths_member", "dc_index", "dc_daily",
    "dc_member", "tdx_index", "tdx_daily", "tdx_member", "kpl_list", "kpl_concept_cons",
})

VERIFIED_SUPER_APIS: Final[frozenset[str]] = frozenset({
    "daily", "stk_auction_o", "stk_auction_c", "ths_index", "moneyflow", "cyq_perf",
    "cyq_chips", "top_list", "top_inst", "stk_factor_pro", "report_rc", "hm_list", "moneyflow_cnt_ths",
    "limit_list_d", "kpl_list",
})


def api_capability(api_name: str) -> ApiCapability:
    if api_name in HISTORICAL_MINUTE_APIS:
        return ApiCapability(
            frequency="historical_minute_file",
            decision_eligible=False,
            preferred_providers=(),
            status="offline_only",
            note="Historical minute or tick history is accepted only from mounted offline files; online gateway download is disabled.",
        )
    if api_name in REALTIME_MARKET_HOURS_APIS:
        return ApiCapability(
            frequency="realtime",
            decision_eligible=False,
            preferred_providers=("super",),
            status="market_hours_only",
            note="The audited super alias selects City or GET per live interface; unsupported realtime families have no candidate and are never inferred from configuration alone.",
        )
    if api_name in SUPER_PREFERRED_APIS:
        return ApiCapability(
            frequency="daily_or_event",
            decision_eligible=api_name in {"moneyflow", "moneyflow_dc", "cyq_perf", "cyq_chips", "stk_factor_pro"},
            preferred_providers=("super", "primary"),
            status="verified" if api_name in VERIFIED_SUPER_APIS else "declared",
            note="Super gateway is preferred for specialty data; empty responses remain source evidence, not a successful signal.",
        )
    if api_name == "stock_basic":
        return ApiCapability(
            frequency="reference_daily",
            decision_eligible=True,
            preferred_providers=("primary", "super", "backup"),
            status="verified",
            note="Reference-data route; the REST backup supports this endpoint only.",
        )
    if api_name == "daily":
        return ApiCapability(
            frequency="daily_or_auction",
            decision_eligible=True,
            preferred_providers=("super_get", "primary"),
            status="verified",
            note="The verified super GET gateway is the canonical equity daily route; the primary provider remains a fallback.",
        )
    if api_name in {"index_daily", "daily_basic", "adj_factor", "stk_limit", "suspend_d", "trade_cal", "stk_auction", "stk_auction_o", "stk_auction_c"}:
        return ApiCapability(
            frequency="daily_or_auction",
            decision_eligible=True,
            preferred_providers=("primary", "super"),
            status="verified" if api_name in {"stk_auction_o", "stk_auction_c"} else "declared",
            note="Primary source is preferred for canonical daily and auction data.",
        )
    if api_name in OFFICIAL_EXTENSIONS:
        spec = official_spec(api_name)
        return ApiCapability(
            frequency=spec.frequency if spec else "event_or_periodic",
            decision_eligible=False,
            preferred_providers=("super", "primary"),
            status="declared",
            note="Official catalog candidate within the configured package boundary; the super route is tried first, but only observed rows verify provider coverage.",
        )
    return ApiCapability(
        frequency="event_or_periodic",
        decision_eligible=False,
        preferred_providers=("primary", "super"),
    )


def provider_order(api_name: str) -> tuple[str, ...]:
    return api_capability(api_name).preferred_providers


def catalog_metadata(api_name: str) -> dict[str, object]:
    contract = api_capability(api_name)
    return {
        "frequency": contract.frequency,
        "decision_eligible": contract.decision_eligible,
        "status": contract.status,
        "preferred_providers": list(contract.preferred_providers),
        "note": contract.note,
    }
