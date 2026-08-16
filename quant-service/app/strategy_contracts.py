"""Stable, serialisable contracts shared by live research and paper replay.

These contracts describe evidence and policy; they do not authorize orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EvidenceRef:
    source: str
    observed_at: datetime | None = None
    available_at: datetime | None = None
    fields: tuple[str, ...] = ()
    quality: str = "unknown"


@dataclass(frozen=True)
class SignalSpec:
    strategy_key: str
    strategy_version: str
    signal_type: str
    symbol: str
    direction: int
    observed_at: datetime
    score: float = 0.0
    evidence: tuple[EvidenceRef, ...] = ()
    risk_flags: tuple[str, ...] = ()
    conditions: dict[str, Any] = field(default_factory=dict)
    horizon_key: str | None = None
    valid_until: datetime | None = None
    expected_return: float | None = None
    probability_profile_id: str | None = None
    invalidation_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactorSpec:
    """Versioned, causal factor definition for live and replay use.

    The contract names the evidence and time semantics; it intentionally does
    not embed a free-form expression language.  The implementation remains a
    reviewed Python/SQL function, which keeps live and replay behaviour
    inspectable and deterministic.
    """

    factor_key: str
    version: str
    frequency: str
    inputs: tuple[str, ...]
    availability_clock: str
    minimum_history: int = 0
    quality_flags: tuple[str, ...] = ()
    live_use: str = "evidence_only"
    # These permissions are intentionally separate from ``live_use``:
    # recording a factor as live evidence is not permission to fit it into a
    # model or let it change a decision score.  Any promotion therefore has
    # an explicit, serialised review boundary.
    training_permitted: bool = False
    inference_permitted: bool = True
    deprecated_at: str | None = None
    description: str = ""


@dataclass(frozen=True)
class MarketEvent:
    """Immutable event envelope for deterministic, provider-free replay."""

    event_id: str
    schema_version: str
    source: str
    symbol: str | None
    event_time: datetime | None
    available_at: datetime
    ingested_at: datetime
    payload_hash: str
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    status: str
    allow_confirmation: bool
    allow_paper: bool
    reasons: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LabelSpec:
    label_key: str = "triple_barrier_v1"
    upper_return: float = 0.03
    lower_return: float = -0.02
    max_horizon_minutes: int = 60
    cost_bps: float = 18.0


def contract_payload(value: Any) -> dict[str, Any]:
    """Convert nested dataclass contracts to JSON-safe dictionaries."""
    if hasattr(value, "__dataclass_fields__"):
        return {key: contract_payload(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [contract_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): contract_payload(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value
