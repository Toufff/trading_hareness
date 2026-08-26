"""Typed provenance contracts for normalized market evidence.

Provider payloads may look similar while having fundamentally different
coverage and decision semantics.  These small contracts make that distinction
explicit before data reaches a strategy or a dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True)
class EvidenceContract:
    """Stable meaning of one normalized evidence stream."""

    key: str
    provider_key: str
    capability: str
    scope: str
    cross_sectional: bool
    decision_eligible: bool
    semantics: str
    schema_version: str = "market-evidence-v1"

    def materialize(self, status: dict[str, Any] | None = None, /, **extra: Any) -> dict[str, Any]:
        """Attach non-negotiable provenance without dropping runtime evidence."""
        observed = dict(status or {})
        observed.update(extra)
        observed.update({
            "evidence_contract": self.key,
            "evidence_schema_version": self.schema_version,
            "provider_key": self.provider_key,
            "capability": self.capability,
            "scope": self.scope,
            "cross_sectional": self.cross_sectional,
            "decision_eligible": self.decision_eligible,
            "semantics": self.semantics,
        })
        return observed


EVIDENCE_CONTRACTS: Final[dict[str, EvidenceContract]] = {
    "fuyao_all_a_snapshot": EvidenceContract(
        key="fuyao_all_a_snapshot", provider_key="fuyao_ths", capability="a_share_prices_snapshot",
        scope="all_a_cross_section", cross_sectional=True, decision_eligible=False,
        semantics="all_a_price_volume_turnover_snapshot_no_main_flow",
    ),
    "tencent_watch_quote": EvidenceContract(
        key="tencent_watch_quote", provider_key="tencent_free", capability="order_book_quote",
        scope="explicit_watchlist_only", cross_sectional=False, decision_eligible=True,
        semantics="same_scan_batched_watch_price_with_exchange_timestamp",
    ),
    "sina_watch_quote": EvidenceContract(
        key="sina_watch_quote", provider_key="sina_free", capability="realtime_quote",
        scope="explicit_watchlist_only", cross_sectional=False, decision_eligible=False,
        semantics="bounded_watch_price_fallback_without_decision_timestamp_contract",
    ),
    # Derived from the same licensed all-A snapshot the price cross-section
    # comes from, so unlike the bounded Eastmoney basket it carries genuine
    # cross-sectional scope.  It deliberately covers only the two fields that
    # are arithmetic definitions; main_net_inflow stays Eastmoney's alone.
    "fuyao_ths_derived_watch_flow": EvidenceContract(
        key="fuyao_ths_derived_watch_flow", provider_key="fuyao_ths", capability="a_share_prices_snapshot",
        scope="all_a_cross_section", cross_sectional=True, decision_eligible=False,
        semantics="volume_ratio_and_turnover_rate_derived_from_licensed_snapshot_volume_and_local_float_shares",
    ),
    "eastmoney_watch_flow": EvidenceContract(
        key="eastmoney_watch_flow", provider_key="eastmoney_free", capability="watchlist_flow_quote",
        scope="explicit_watchlist_only", cross_sectional=False, decision_eligible=False,
        semantics="watchlist_public_flow_proxy_not_exchange_order_flow",
    ),
}


def evidence_contract(key: str) -> EvidenceContract:
    """Return one declared contract and fail early on an unreviewed stream."""
    try:
        return EVIDENCE_CONTRACTS[key]
    except KeyError as error:
        raise ValueError(f"unknown evidence contract: {key}") from error


def materialize_evidence_status(key: str, status: dict[str, Any] | None = None, /, **extra: Any) -> dict[str, Any]:
    """Attach a contract to a secret-free runtime source status."""
    return evidence_contract(key).materialize(status, **extra)


def evidence_contract_catalog() -> list[dict[str, Any]]:
    """Return a deterministic, secret-free catalog for APIs and agents."""
    return [
        {
            "key": contract.key,
            "provider_key": contract.provider_key,
            "capability": contract.capability,
            "scope": contract.scope,
            "cross_sectional": contract.cross_sectional,
            "decision_eligible": contract.decision_eligible,
            "semantics": contract.semantics,
            "schema_version": contract.schema_version,
        }
        for contract in sorted(EVIDENCE_CONTRACTS.values(), key=lambda item: item.key)
    ]


__all__ = [
    "EVIDENCE_CONTRACTS", "EvidenceContract", "evidence_contract", "evidence_contract_catalog",
    "materialize_evidence_status",
]
