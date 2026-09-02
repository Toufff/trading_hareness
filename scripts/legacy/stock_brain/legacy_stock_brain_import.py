"""Convert trustworthy stock-brain broker facts into the new portfolio contract.

Only the latest read-only CITIC observation is accepted.  Legacy plans,
triggers, candidates and action-card state are intentionally ignored.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

# This module moved out of the ``app`` package (it is one-way migration code,
# not a production dependency) but still needs one app/ contract type.
_QUANT_SERVICE_ROOT = Path(__file__).resolve().parents[3] / "quant-service"
if str(_QUANT_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_QUANT_SERVICE_ROOT))

from app.personal_decision_contracts import BrokerPortfolioSnapshotInput  # noqa: E402


class LegacyPortfolioImportError(ValueError):
    """The legacy artifact cannot establish an exact broker observation."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as error:
        raise LegacyPortfolioImportError(f"{field} is not numeric") from error


def _symbol(code: str) -> str:
    normalized = code.strip().lower()
    if len(normalized) != 8 or normalized[:2] not in {"sh", "sz", "bj"} or not normalized[2:].isdigit():
        raise LegacyPortfolioImportError(f"unsupported legacy security code: {code!r}")
    return f"{normalized[2:]}.{normalized[:2].upper()}"


def load_stock_brain_portfolio(
    config_path: str | Path,
    *,
    account_key: str = "citics-primary",
    require_evidence: bool = True,
) -> BrokerPortfolioSnapshotInput:
    """Load and validate one exact CITIC snapshot from ``daily/config.json``."""
    path = Path(config_path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise LegacyPortfolioImportError(f"cannot read legacy portfolio config: {path}") from error

    account = raw.get("broker_account")
    positions = raw.get("positions")
    if not isinstance(account, dict) or not isinstance(positions, list):
        raise LegacyPortfolioImportError("broker_account and positions are required")
    observed_at = str(account.get("observed_at") or raw.get("portfolio_observed_at") or "")
    if not observed_at or observed_at != str(raw.get("portfolio_observed_at") or ""):
        raise LegacyPortfolioImportError("account and portfolio observation times do not match")
    account_source = str(account.get("source") or "")
    if "CITIC" not in account_source or "readonly" not in account_source.lower():
        raise LegacyPortfolioImportError("snapshot is not a CITIC read-only broker fact")

    evidence_raw = str(account.get("evidence_path") or "")
    evidence_path = Path(evidence_raw) if evidence_raw else None
    if require_evidence and (evidence_path is None or not evidence_path.is_file()):
        raise LegacyPortfolioImportError("broker screenshot evidence is missing")

    converted_positions: list[dict[str, Any]] = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise LegacyPortfolioImportError(f"positions[{index}] is not an object")
        position_observed_at = str(position.get("observed_at") or "")
        position_source = str(position.get("source") or "")
        if position_observed_at != observed_at:
            raise LegacyPortfolioImportError(f"positions[{index}] has a different observation time")
        if "CITIC" not in position_source:
            raise LegacyPortfolioImportError(f"positions[{index}] is not broker-sourced")
        quantity = _decimal(position.get("quantity"), f"positions[{index}].quantity")
        sellable = _decimal(position.get("available_quantity"), f"positions[{index}].available_quantity")
        market_value = _decimal(position.get("observed_market_value"), f"positions[{index}].observed_market_value")
        raw_weight = str(position.get("weight") or "").strip().removesuffix("%")
        converted_positions.append({
            "symbol": _symbol(str(position.get("code") or "")),
            "name": str(position.get("name") or "").strip(),
            "quantity": quantity,
            "sellable_quantity": sellable,
            "average_cost": _decimal(position.get("cost"), f"positions[{index}].cost"),
            "market_price": _decimal(position.get("observed_price"), f"positions[{index}].observed_price"),
            "market_value": market_value,
            "unrealized_pnl": _decimal(position.get("observed_profit"), f"positions[{index}].observed_profit"),
            "position_weight_pct": _decimal(raw_weight, f"positions[{index}].weight"),
            "metadata": {
                "legacy_code": position.get("code"),
                "legacy_observed_at": position_observed_at,
                "legacy_source": position_source,
                "observed_profit_percent": position.get("observed_profit_percent"),
            },
        })

    market_value = _decimal(account.get("market_value"), "broker_account.market_value")
    position_total = sum((item["market_value"] for item in converted_positions), Decimal("0"))
    return BrokerPortfolioSnapshotInput.model_validate({
        "account_key": account_key,
        "source": "stock_brain.citics_mumu",
        "source_snapshot_key": f"stock-brain:{observed_at}",
        "observed_at": observed_at,
        "verification": "verified_exact",
        "cash": _decimal(account.get("available_cash"), "broker_account.available_cash"),
        "total_asset": _decimal(account.get("total_assets"), "broker_account.total_assets"),
        "total_market_value": market_value,
        "positions": converted_positions,
        "metadata": {
            "legacy_config_path": str(path),
            "evidence_path": str(evidence_path) if evidence_path else None,
            "legacy_source": account_source,
            "withdrawable_cash": account.get("withdrawable_cash"),
            "position_percent": account.get("position_percent"),
            "market_value_reconciliation_delta": str(market_value - position_total),
            "migration_policy": "broker_facts_only; legacy_plans_and_triggers_discarded",
        },
    })


__all__ = ["LegacyPortfolioImportError", "load_stock_brain_portfolio"]
