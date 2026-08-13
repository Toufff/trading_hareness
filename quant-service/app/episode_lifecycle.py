"""Pure and database-backed lifecycle helpers for intraday signal episodes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json


EPISODE_CONTINUITY = timedelta(minutes=5)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def strategy_family(signal_key: str) -> str:
    """Collapse stage-specific keys into one primary setup family."""
    value = str(signal_key or "")
    for marker, family in (
        ("upside_acceptance_eac", "upside_breakout_eac"),
        ("upside_breakout_eac", "upside_breakout_eac"),
        ("deep_reversal", "deep_reversal"),
        ("green_reclaim", "green_reclaim"),
        ("sector_surge", "sector_surge"),
        ("extreme_flow", "extreme_flow"),
        ("price_extension", "price_extension"),
        ("leader_burst", "leader_burst"),
        ("volume_anomaly", "volume_anomaly"),
    ):
        if marker in value:
            return family
    parts = value.split(":")
    return parts[2] if len(parts) >= 3 and parts[2] else value or "unknown"


def signal_direction(signal: dict[str, Any]) -> int:
    signal_type = str(signal.get("signal_type") or "watch")
    return -1 if signal_type in {"reduce", "exit"} else 1


def signal_stage(signal: dict[str, Any]) -> str:
    conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    setup = str(conditions.get("setup") or "")
    if "acceptance" in setup or signal.get("stage_upgrade"):
        return "acceptance"
    if "reclaim" in setup or "reversal" in setup:
        return "reclaim"
    if signal.get("signal_type") in {"reduce", "exit"}:
        return "failure"
    if "breakout" in setup or "surge" in setup or "burst" in setup:
        return "expansion"
    return "detected"


def material_state_payload(signal: dict[str, Any]) -> dict[str, Any]:
    conditions = signal.get("conditions") if isinstance(signal.get("conditions"), dict) else {}
    numeric = {}
    for key in ("price", "pct_change", "volume_ratio", "turnover_rate", "main_net_inflow"):
        value = conditions.get(key)
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None:
            # The hash is a lifecycle discriminator, not a price feature.  It
            # deliberately uses coarse bins so every scan is not a new event.
            step = 0.005 if key == "price" else 0.5 if key in {"volume_ratio", "turnover_rate"} else 1.0
            numeric[key] = round(number / step) * step
    return {
        "strategy_family": strategy_family(str(signal.get("signal_key") or "")),
        "direction": signal_direction(signal),
        "stage": signal_stage(signal),
        "setup": conditions.get("setup"),
        "numeric": numeric,
        "market_state": (conditions.get("policy_gate") or {}).get("market_state") if isinstance(conditions.get("policy_gate"), dict) else None,
    }


def material_state_hash(signal: dict[str, Any]) -> str:
    payload = json.dumps(material_state_payload(signal), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def session_date(observed_at: datetime) -> date:
    return observed_at.astimezone(SHANGHAI).date()


def ensure_signal_episode(connection: Any, signal: dict[str, Any], observed_at: datetime,
                          event_state: str) -> dict[str, Any]:
    symbol = str(signal["symbol"])
    family = strategy_family(str(signal.get("signal_key") or ""))
    version = str(signal.get("strategy_version") or "live-research-contract-v1")
    direction = signal_direction(signal)
    exchange_date = session_date(observed_at)
    stage = signal_stage(signal)
    state_hash = material_state_hash(signal)
    active = connection.execute(
        """SELECT * FROM quant.intraday_signal_episodes
             WHERE symbol=%s AND strategy_key=%s AND strategy_version=%s AND direction=%s
               AND session_date=%s AND state='active'
             ORDER BY last_observed_at DESC LIMIT 1""",
        (symbol, family, version, direction, exchange_date),
    ).fetchone()
    if active is not None and observed_at - active["last_observed_at"] <= EPISODE_CONTINUITY:
        connection.execute(
            """UPDATE quant.intraday_signal_episodes
                  SET last_observed_at=%s,stage=%s,material_state_hash=%s,
                      evidence=evidence || %s::jsonb,updated_at=now()
                WHERE episode_id=%s""",
            (observed_at, stage, state_hash, Json({"last_event_state": event_state}), active["episode_id"]),
        )
        return {"episode_id": str(active["episode_id"]), "material_state_hash": state_hash,
                "stage": stage, "state": "active", "rearm": False}
    if active is not None:
        connection.execute(
            """UPDATE quant.intraday_signal_episodes SET state='cleared',clear_at=%s,clear_reason=%s,updated_at=now()
                WHERE episode_id=%s""",
            (observed_at, "continuity_window_elapsed", active["episode_id"]),
        )
    row = connection.execute(
        """INSERT INTO quant.intraday_signal_episodes(
             symbol,strategy_key,strategy_version,direction,session_date,state,stage,material_state_hash,
             first_observed_at,last_observed_at,rearm_count,evidence)
           VALUES(%s,%s,%s,%s,%s,'active',%s,%s,%s,%s,%s,%s)
           RETURNING episode_id""",
        (symbol, family, version, direction, exchange_date, stage, state_hash, observed_at, observed_at,
         1 if active is not None else 0, Json({"first_event_state": event_state})),
    ).fetchone()
    return {"episode_id": str(row["episode_id"]), "material_state_hash": state_hash,
            "stage": stage, "state": "active", "rearm": active is not None}


def clear_stale_signal_episodes(connection: Any, symbols: list[str], observed_at: datetime) -> int:
    if not symbols:
        return 0
    row = connection.execute(
        """UPDATE quant.intraday_signal_episodes
              SET state='cleared',clear_at=%s,clear_reason='signal_condition_cleared',updated_at=now()
            WHERE symbol=ANY(%s) AND state='active' AND last_observed_at < %s
        RETURNING episode_id""",
        (observed_at, symbols, observed_at - EPISODE_CONTINUITY),
    )
    return len(row.fetchall())


__all__ = ["EPISODE_CONTINUITY", "clear_stale_signal_episodes", "ensure_signal_episode",
           "material_state_hash", "material_state_payload", "session_date", "signal_stage", "strategy_family"]
