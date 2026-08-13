"""Deterministic A-share paper execution primitives.

The module is deliberately broker-free.  It models tradability and costs for
research proposals only; a caller must explicitly opt into any future paper
fill simulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable

from psycopg.types.json import Json

from .episode_lifecycle import strategy_family


LOT_SIZE = 100
DEFAULT_COMMISSION_RATE = Decimal("0.0003")
DEFAULT_STAMP_TAX_RATE = Decimal("0.001")
DEFAULT_SLIPPAGE_BPS = Decimal("5")


@dataclass(frozen=True)
class PaperTradability:
    allowed: bool
    reasons: tuple[str, ...] = ()


def round_lot(quantity: int | float | Decimal, lot_size: int = LOT_SIZE) -> int:
    value = Decimal(str(quantity or 0))
    return int((value / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size)


def paper_tradability(*, side: str, requested_quantity: int, quote: dict[str, Any] | None,
                      position: dict[str, Any] | None = None) -> PaperTradability:
    quote = quote or {}
    reasons: list[str] = []
    if requested_quantity <= 0:
        reasons.append("non_positive_quantity")
    if quote.get("is_suspended"):
        reasons.append("suspended")
    if side == "sell" and int((position or {}).get("sellable_quantity") or 0) < requested_quantity:
        reasons.append("t_plus_one_or_insufficient_sellable_quantity")
    pct = float(quote.get("pct_change") or 0)
    if side == "buy" and bool(quote.get("at_limit_up")):
        reasons.append("limit_up_non_fill_risk")
    if side == "sell" and bool(quote.get("at_limit_down")):
        reasons.append("limit_down_non_fill_risk")
    # Explicit flags take precedence; percent is only a conservative fallback.
    if side == "buy" and pct >= 9.8 and not quote.get("allow_limit_fill"):
        reasons.append("limit_up_non_fill_risk")
    if side == "sell" and pct <= -9.8 and not quote.get("allow_limit_fill"):
        reasons.append("limit_down_non_fill_risk")
    return PaperTradability(not reasons, tuple(dict.fromkeys(reasons)))


def estimate_cost(*, side: str, quantity: int, price: Decimal | float,
                  commission_rate: Decimal = DEFAULT_COMMISSION_RATE,
                  stamp_tax_rate: Decimal = DEFAULT_STAMP_TAX_RATE,
                  slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS) -> dict[str, Decimal]:
    notional = Decimal(str(price)) * Decimal(max(0, quantity))
    commission = max(Decimal("5"), notional * commission_rate) if notional else Decimal("0")
    stamp = notional * stamp_tax_rate if side.lower() == "sell" else Decimal("0")
    slippage = notional * slippage_bps / Decimal("10000")
    return {"notional": notional, "commission": commission, "stamp_tax": stamp, "slippage": slippage,
            "total_cost": commission + stamp + slippage}


def triple_barrier_label(path: Iterable[dict[str, Any]], *, entry_price: Decimal | float,
                         entry_at: datetime, spec: Any) -> dict[str, Any]:
    """Label a point-in-time path without looking beyond the configured horizon."""
    entry = Decimal(str(entry_price))
    upper = entry * (Decimal("1") + Decimal(str(spec.upper_return)))
    lower = entry * (Decimal("1") + Decimal(str(spec.lower_return)))
    deadline = entry_at.timestamp() + int(spec.max_horizon_minutes) * 60
    last = None
    for row in path:
        at = row.get("observed_at") or row.get("time")
        if isinstance(at, str):
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if not isinstance(at, datetime) or at.timestamp() < entry_at.timestamp() or at.timestamp() > deadline:
            continue
        close = Decimal(str(row.get("close") or row.get("price") or 0))
        if not close:
            continue
        last = (at, close)
        if close >= upper:
            return {"status": "matured", "label": "upper", "exit_at": at, "exit_price": close,
                    "return": float(close / entry - 1)}
        if close <= lower:
            return {"status": "matured", "label": "lower", "exit_at": at, "exit_price": close,
                    "return": float(close / entry - 1)}
    if last is None:
        return {"status": "unavailable", "label": None, "reason": "no_point_in_time_path"}
    at, close = last
    if at.timestamp() < deadline:
        return {"status": "pending", "label": None, "last_at": at, "last_price": close}
    return {"status": "matured", "label": "time", "exit_at": at, "exit_price": close,
            "return": float(close / entry - 1)}


def paper_decision_payload(signal: dict[str, Any], state: str, policy: dict[str, Any],
                           portfolio_gate: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a research proposal; this function has no broker side effects."""
    signal_type = str(signal.get("signal_type") or "watch")
    direction = 1 if signal_type == "entry" else -1 if signal_type in {"reduce", "exit"} else 0
    key = str(signal.get("signal_key") or "unknown")
    strategy_key = strategy_family(key)
    policy_flags = tuple(str(item) for item in policy.get("risk_flags", ()))
    portfolio_gate = portfolio_gate if isinstance(portfolio_gate, dict) else {}
    blocked = state != "confirmed" or not policy.get("allow_confirmation") or not portfolio_gate.get("allowed", True)
    return {
        "strategy_key": strategy_key,
        "strategy_version": "live-research-contract-v1",
        "symbol": str(signal["symbol"]),
        "direction": direction,
        "status": "proposed" if not blocked else "blocked",
        "decision_at": signal.get("observed_at"),
        "target_quantity": 0,
        "target_weight": float(portfolio_gate.get("target_weight") or 0),
        "evidence": {"signal": signal.get("conditions", {}), "state": state,
                      "boundary": "paper_only_no_automatic_order"},
        "risk_flags": list(dict.fromkeys([*signal.get("risk_flags", ()), *policy_flags,
                                           *portfolio_gate.get("risk_flags", ()),
                                           "paper_only", "manual_review_required"])),
    }


def persist_paper_decision(connection: Any, signal_event_id: Any, payload: dict[str, Any]) -> bool:
    row = connection.execute(
        """INSERT INTO quant.paper_decisions(
             signal_event_id,strategy_key,strategy_version,symbol,direction,status,decision_at,
             target_quantity,target_weight,evidence,risk_flags)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(signal_event_id,strategy_key,strategy_version) DO NOTHING
           RETURNING decision_id""",
        (signal_event_id, payload["strategy_key"], payload["strategy_version"], payload["symbol"],
         payload["direction"], payload["status"], payload["decision_at"], payload["target_quantity"],
         payload["target_weight"], Json(payload["evidence"]), Json(payload["risk_flags"])),
    ).fetchone()
    return row is not None


def persist_barrier_outcome(connection: Any, signal_event_id: Any, *, spec: Any,
                            entry_at: datetime, entry_price: Decimal | float,
                            result: dict[str, Any], source_status: dict[str, Any] | None = None) -> None:
    exit_at = result.get("exit_at") or result.get("last_at")
    exit_price = result.get("exit_price") or result.get("last_price")
    connection.execute(
        """INSERT INTO quant.paper_barrier_outcomes(
             signal_event_id,label_key,upper_return,lower_return,max_horizon_minutes,entry_observed_at,entry_price,
             exit_observed_at,exit_price,label,raw_return,maximum_favorable_excursion,maximum_adverse_excursion,
             status,source_status)
           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(signal_event_id) DO UPDATE SET exit_observed_at=EXCLUDED.exit_observed_at,
             exit_price=EXCLUDED.exit_price,label=EXCLUDED.label,raw_return=EXCLUDED.raw_return,
             maximum_favorable_excursion=EXCLUDED.maximum_favorable_excursion,
             maximum_adverse_excursion=EXCLUDED.maximum_adverse_excursion,status=EXCLUDED.status,
             source_status=EXCLUDED.source_status,calculated_at=now()""",
        (signal_event_id, str(spec.label_key), spec.upper_return, spec.lower_return, spec.max_horizon_minutes,
         entry_at, entry_price, exit_at, exit_price, result.get("label"), result.get("return"),
         result.get("maximum_favorable_excursion"), result.get("maximum_adverse_excursion"),
         str(result.get("status") or "unavailable"), Json(source_status or {})),
    )
