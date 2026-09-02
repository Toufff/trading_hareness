"""Per-scan xiaojie leader-pool evaluation, alerting and launch-radar capture.

Research-only throughout: emitted signal events carry a dedicated stage so
nothing on the decision path can mistake them for watchlist alerts, and the
strategy stays at zero live weight in the promotion registry.  The composition
root (``app/main.py``) still owns session-reference caching and the two
mutable per-session state dicts (``ma5_break_state``/``velocity_state``) so
their lifecycle stays visible next to the rest of the xiaojie wiring; this
module owns the pool evaluation, alert budget and persistence for one scan.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Json

from .launch_radar import evaluate_launch_radar, record_launch_observations as record_launch_radar_observations
from .xiaojie_indicators import evaluate_pool as evaluate_xiaojie_leader_pool, leader_pool as leader_pool_symbols
from .xiaojie_leader_flow import MODEL_VERSION as XIAOJIE_LEADER_FLOW_MODEL_VERSION, alert_priority as xiaojie_alert_priority
from .xiaojie_observation_repository import (
    alerted_count as xiaojie_alerted_count,
    mark_alerted as mark_xiaojie_alerted,
    record_candidates as record_xiaojie_candidates,
)

#: Alerts are per newly-appearing (symbol, mode); this bounds a pathological
#: day.  The running tally is read from the observations table, not held
#: here, so a restart cannot reset it.
XIAOJIE_MAX_ALERTS_PER_SCAN = 5
XIAOJIE_MAX_ALERTS_PER_SESSION = 40


@dataclass(frozen=True)
class XiaojieLeaderFlowDependencies:
    database: Any
    run_database: Callable[..., Awaitable[Any]]
    session_context: Callable[[date], Awaitable[dict[str, Any]]]
    ma5_break_state: dict[str, Any]
    velocity_state: dict[str, Any]
    deliver_alert: Callable[[uuid.UUID, str], Awaitable[dict[str, Any]]]
    alert_text: Callable[[dict[str, Any], date, Mapping[str, str] | None], str]
    safe_error_detail: Callable[[str, int], str]


def _with_connection(dependencies: XiaojieLeaderFlowDependencies, action: Callable[[Any], Any]) -> Any:
    with dependencies.database.transaction() as connection:
        return action(connection)


def persist_xiaojie_signal_event(connection: Any, scan_id: uuid.UUID, observed_at: datetime,
                                  candidate: dict[str, Any]) -> uuid.UUID:
    """Record a research observation as a distinctly-staged signal event.

    ``stage`` isolates it: every decision-path consumer selects on the stages
    the watchlist scan emits, so a research row cannot be mistaken for one.
    """
    event_id = uuid.uuid4()
    mode = str(candidate.get("mode") or "unclassified")
    connection.execute(
        """INSERT INTO quant.intraday_signal_events(
                signal_event_id,scan_id,symbol,signal_key,signal_type,severity,state,score,
                observed_at,conditions,evidence,risk_flags,stage)
           VALUES(%s,%s,%s,%s,'watch','info','alerted',0,%s,%s,%s,%s,'xiaojie_leader_flow_research')""",
        (event_id, scan_id, candidate["symbol"], f"{candidate['symbol']}:xiaojie:{mode}",
         observed_at, Json({"mode": mode, "position": candidate.get("position") or {},
                            "stop_loss": candidate.get("stop_loss") or {}}),
         Json(candidate.get("evidence") or {}), Json(candidate.get("risk_flags") or [])),
    )
    return event_id


async def run_xiaojie_leader_flow(*, scan_id: uuid.UUID, observed_at: datetime,
                                   all_a_rows: list[dict[str, Any]],
                                   dependencies: XiaojieLeaderFlowDependencies) -> dict[str, Any]:
    """Evaluate the leader pool from this scan's own cross-section."""
    if not all_a_rows:
        return {"status": "skipped", "reason": "no all-A cross-section in this scan"}
    trading_date = observed_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
    reference = await dependencies.session_context(trading_date)
    if not reference.get("limits"):
        return {"status": "blocked", "reason": "session trade limits unavailable"}
    result = evaluate_xiaojie_leader_pool(
        all_a_rows, limits=reference["limits"], membership=reference["membership"],
        references=reference["references"], observed_at=observed_at,
        ma5_break_state=dependencies.ma5_break_state,
        market_volume_baseline=reference.get("market_volume_baseline"),
    )
    candidates = result["candidates"]
    fresh = await dependencies.run_database(
        lambda: _with_connection(dependencies, lambda connection: record_xiaojie_candidates(
            connection, trading_date, observed_at, scan_id, candidates)),
        timeout_seconds=60,
    ) if candidates else []

    # A board already locked at the limit cannot be acted on: measured across
    # 104 observations on 2026-08-27, the 61 found already sealed produced 0
    # gains, 57 unchanged and 4 losses from the moment they were flagged, while
    # the 43 found unsealed averaged +0.40%.  They stay recorded as research
    # evidence but must not consume a scarce alert slot.
    actionable = [item for item in fresh
                  if not ((item.get("evidence") or {}).get("board") or {}).get("sealed")]
    sealed_skipped = len(fresh) - len(actionable)
    # The budget is read from what the table already recorded, so a restart
    # mid-session cannot hand out a fresh allowance.
    sent = await dependencies.run_database(
        lambda: _with_connection(dependencies, lambda connection: xiaojie_alerted_count(connection, trading_date)),
        timeout_seconds=30,
    )
    remaining = min(XIAOJIE_MAX_ALERTS_PER_SCAN, max(0, XIAOJIE_MAX_ALERTS_PER_SESSION - sent))
    # Alert slots are scarce, so they go to the highest-conviction setups
    # rather than to whichever mode happens to be most numerous.
    actionable = sorted(actionable, key=xiaojie_alert_priority)
    alerted: list[tuple[str, str]] = []
    alert_errors: list[str] = []
    for candidate in actionable[:remaining]:
        try:
            event_id = await dependencies.run_database(
                lambda item=candidate: _with_connection(
                    dependencies, lambda connection: persist_xiaojie_signal_event(connection, scan_id, observed_at, item)),
                timeout_seconds=30,
            )
            await dependencies.deliver_alert(
                event_id, dependencies.alert_text(candidate, trading_date, reference.get("names")))
            alerted.append((candidate["symbol"], str(candidate.get("mode") or "unclassified")))
        except Exception as error:  # noqa: BLE001 - an alert failure must not end the scan
            alert_errors.append(f"{candidate.get('symbol')}: {dependencies.safe_error_detail(str(error), 160)}")
    if alerted:
        await dependencies.run_database(
            lambda: _with_connection(dependencies, lambda connection: mark_xiaojie_alerted(
                connection, trading_date, observed_at, alerted)),
            timeout_seconds=30,
        )
    # Shadow-mode launch radar rides the same cross-section: the launch band
    # (past +5%, not yet leader-pool territory) is watched for the three-way
    # coincidence of volume burst, standing sector anchor and price velocity.
    # Research-only - observations settle through the shared outcomes table,
    # and no alert is ever sent from here.
    launch_status: dict[str, Any] = {"status": "skipped"}
    try:
        launch = evaluate_launch_radar(
            all_a_rows, limits=reference["limits"], membership=reference["membership"],
            references=reference["references"], pool=leader_pool_symbols(all_a_rows, reference["limits"]),
            velocity_state=dependencies.velocity_state, observed_at=observed_at,
            elapsed_session_minutes=int(result["market_gate"].get("elapsed_session_minutes") or 0),
        )
        launch_fresh = await dependencies.run_database(
            lambda: _with_connection(dependencies, lambda connection: record_launch_radar_observations(
                connection, trading_date, observed_at, scan_id, launch["candidates"])),
            timeout_seconds=30,
        ) if launch["candidates"] else 0
        launch_status = {"status": "completed", "band_size": launch["band_size"],
                         "candidates": len(launch["candidates"]), "new": launch_fresh,
                         "truncated": launch["truncated"]}
    except Exception as error:  # noqa: BLE001 - the radar must never end the scan
        launch_status = {"status": "failed", "reason": dependencies.safe_error_detail(str(error), 200)}
    return {
        "status": "completed", "model_version": XIAOJIE_LEADER_FLOW_MODEL_VERSION,
        "launch_radar": launch_status,
        "pool_size": result["pool_size"], "evaluated": result["evaluated"],
        "main_sector_count": result["main_sector_count"],
        "regime": result["regime"],
        "candidates": len(candidates), "new_candidates": len(fresh), "alerted": len(alerted),
        "actionable_candidates": len(actionable),
        "sealed_skipped": sealed_skipped,
        "alerts_suppressed_by_cap": max(0, len(actionable) - len(alerted)),
        "alerted_modes": sorted({mode for _symbol, mode in alerted}),
        "alerts_sent_this_session": sent + len(alerted),
        "alert_errors": alert_errors or None,
        "reference_symbols": len(reference["limits"]),
        "live_effect": "none", "boundary": "research_only; no_automatic_order",
    }


__all__ = [
    "XIAOJIE_MAX_ALERTS_PER_SCAN",
    "XIAOJIE_MAX_ALERTS_PER_SESSION",
    "XiaojieLeaderFlowDependencies",
    "persist_xiaojie_signal_event",
    "run_xiaojie_leader_flow",
]
