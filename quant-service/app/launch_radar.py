"""Shadow-mode launch radar: names being attacked toward the board, pre-limit.

The leader pool only admits names within NEAR_LIMIT_PCT of the limit, so the
launch segment - the move the user actually wants warned about - is invisible
until it is nearly over.  Measured on 2026-08-26..28 minute bars, crossing
+5% preceded the seal by a median 15 minutes (53 of 59 sealed boards had a
window), which the 10-30s scan cadence samples 30-90 times.

The base rate is the danger: 468-779 names crossed +5% per session and only
10-14% sealed.  Raw threshold alerts would be ~500 wrong pushes a day, so
admission requires three concurrent signs measured from data already in the
scan: a volume burst against the name's own 5-day baseline, a sealed anchor
already standing in one of its concepts, and price velocity over the last
few minutes.  Even so this stays research-only: observations settle through
the same outcomes table as the leader flow, and only a week of measured
precision decides whether it ever earns an alert slot.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Mapping

from psycopg.types.json import Json

from .xiaojie_indicators import LIMIT_TOLERANCE, NEAR_LIMIT_PCT, SESSION_MINUTES, board_state, snapshot_fields

MODEL_VERSION = "launch-radar-v1"
#: Velocity starts warming here, below the band: a name is admitted the scan
#: it crosses +5%, which is exactly when its speed matters - tracking only
#: band members would leave the window cold at the crossing moment.
WARMUP_MIN_PCT = 3.0
#: The launch band starts where a move stops being noise...
LAUNCH_MIN_PCT = 5.0
#: ...and hands over to the leader pool where that pool begins.
LAUNCH_VOLUME_RATIO_MIN = 2.0
#: At least one sealed board already standing in a shared concept.
LAUNCH_MIN_SECTOR_SEALS = 1
#: Velocity: this much gained inside the tracking window.
LAUNCH_VELOCITY_MIN_PCT = 1.5
LAUNCH_VELOCITY_WINDOW_SECONDS = 180.0
#: Bound one scan's output; ordered by velocity so truncation drops the slowest.
MAX_LAUNCH_ROWS = 40


def track_velocity(state: dict[str, list[tuple[float, float]]], symbol: str,
                   price: float, observed_at: datetime) -> float | None:
    """Percent gained inside the window, from in-process scan history.

    The first sighting returns None - a name must be seen twice inside the
    window before its speed is a fact rather than a guess.  Entries older
    than the window are pruned, so a collector restart simply restarts the
    measurement instead of comparing against a stale price.
    """
    stamp = observed_at.timestamp()
    history = [item for item in state.get(symbol, ()) if stamp - item[0] <= LAUNCH_VELOCITY_WINDOW_SECONDS]
    history.append((stamp, price))
    state[symbol] = history
    if len(history) < 2 or history[0][1] <= 0:
        return None
    return (price / history[0][1] - 1) * 100


def sealed_sector_counts(rows_by_symbol: Mapping[str, Mapping[str, Any]],
                         pool: list[str], limits: Mapping[str, float],
                         membership: Mapping[str, set[str]]) -> dict[str, int]:
    """Sealed boards per concept, from the same cross-section the scan holds."""
    counts: dict[str, int] = {}
    for symbol in pool:
        fields = snapshot_fields(rows_by_symbol.get(symbol) or {})
        if not board_state(fields, limits.get(symbol))["sealed"]:
            continue
        for sector in membership.get(symbol, set()):
            counts[sector] = counts.get(sector, 0) + 1
    return counts


def evaluate_launch_radar(
    rows: list[dict[str, Any]], *,
    limits: Mapping[str, float],
    membership: Mapping[str, set[str]],
    references: Mapping[str, Mapping[str, Any]],
    pool: list[str],
    velocity_state: dict[str, list[tuple[float, float]]],
    observed_at: datetime,
    elapsed_session_minutes: int,
) -> dict[str, Any]:
    """One scan's launch candidates: in the band, bursting, anchored, moving."""
    rows_by_symbol = {str(row.get("symbol") or ""): row for row in rows}
    anchors = sealed_sector_counts(rows_by_symbol, pool, limits, membership)
    in_pool = set(pool)
    candidates: list[dict[str, Any]] = []
    scanned = 0
    for row in rows:
        symbol = str(row.get("symbol") or "")
        limit_up = limits.get(symbol)
        pct = row.get("pct_change")
        if not symbol or limit_up is None or limit_up <= 0 or pct is None or pct < WARMUP_MIN_PCT:
            continue
        fields = snapshot_fields(row)
        price = fields.get("price")
        if price is None or price <= 0:
            continue
        # Velocity warms from WARMUP_MIN_PCT so the window already holds a
        # reference price the scan a name crosses into the band.
        velocity = track_velocity(velocity_state, symbol, float(price), observed_at)
        if pct < LAUNCH_MIN_PCT:
            continue
        distance = (limit_up - max(price, fields.get("high") or price)) / limit_up * 100
        if symbol in in_pool or distance <= NEAR_LIMIT_PCT:
            continue  # the leader pool's territory, not a launch
        scanned += 1
        reference = references.get(symbol) or {}
        mean_volume = reference.get("mean_volume_5d")
        volume_ratio = None
        if mean_volume and row.get("volume") and elapsed_session_minutes > 0:
            expected = float(mean_volume) * (elapsed_session_minutes / SESSION_MINUTES)
            if expected > 0:
                volume_ratio = float(row["volume"]) / expected
        anchor_sectors = {sector: anchors[sector]
                         for sector in membership.get(symbol, set())
                         if anchors.get(sector, 0) >= LAUNCH_MIN_SECTOR_SEALS}
        if (velocity is None or velocity < LAUNCH_VELOCITY_MIN_PCT
                or volume_ratio is None or volume_ratio < LAUNCH_VOLUME_RATIO_MIN
                or not anchor_sectors):
            continue
        candidates.append({
            "symbol": symbol, "mode": "launch_radar", "decision": "launch_watch",
            "evidence": {
                "price": float(price), "pct_change": float(pct),
                "distance_to_limit_pct": round(distance, 3),
                "velocity_pct": round(velocity, 3),
                "velocity_window_seconds": LAUNCH_VELOCITY_WINDOW_SECONDS,
                "volume_ratio": round(volume_ratio, 3),
                "anchor_sectors": {key: int(value) for key, value in sorted(anchor_sectors.items())},
                # Settlement reads price and board.sealed; a launch candidate
                # is below the limit by construction, so it is always evaluable.
                "board": {"sealed": False, "touched": bool(distance <= 0), "broken": False},
                "source": "launch_radar_v1",
            },
        })
    candidates.sort(key=lambda item: -item["evidence"]["velocity_pct"])
    return {"band_size": scanned, "candidates": candidates[:MAX_LAUNCH_ROWS],
            "truncated": max(0, len(candidates) - MAX_LAUNCH_ROWS),
            "anchored_sectors": len(anchors)}


def record_launch_observations(connection: Any, trading_date: date, observed_at: datetime,
                               scan_id: Any, candidates: list[dict[str, Any]]) -> int:
    """Upsert this scan's launch sightings; a held launch widens one row.

    Mirrors the leader-flow observation shape so the existing settlement and
    scorecard read these rows with no schema of their own - they appear as
    mode ``launch_radar`` alongside the strategy's modes.
    """
    fresh = 0
    for candidate in candidates:
        row = connection.execute(
            """INSERT INTO quant.xiaojie_leader_flow_observations(
                    trading_date,symbol,mode,model_version,first_seen_at,last_seen_at,
                    observation_count,first_scan_id,decision,risk_flags,reasons,market_gate,
                    first_evidence,last_evidence)
               SELECT %s,%s,'launch_radar',%s,%s,%s,1,%s,%s,'[]'::jsonb,'[]'::jsonb,'{}'::jsonb,%s,%s
                WHERE EXISTS(SELECT 1 FROM quant.instruments WHERE symbol=%s)
               ON CONFLICT(trading_date,symbol,mode) DO UPDATE SET
                 last_seen_at=EXCLUDED.last_seen_at,
                 observation_count=quant.xiaojie_leader_flow_observations.observation_count+1,
                 last_evidence=EXCLUDED.last_evidence
               RETURNING (xmax = 0) AS inserted""",
            (trading_date, candidate["symbol"], MODEL_VERSION, observed_at, observed_at,
             scan_id, candidate.get("decision") or "launch_watch",
             Json(candidate.get("evidence") or {}), Json(candidate.get("evidence") or {}),
             candidate["symbol"]),
        ).fetchone()
        if row is not None and row["inserted"]:
            fresh += 1
    return fresh


__all__ = [
    "LAUNCH_MIN_PCT", "LAUNCH_MIN_SECTOR_SEALS", "LAUNCH_VELOCITY_MIN_PCT",
    "LAUNCH_VELOCITY_WINDOW_SECONDS", "LAUNCH_VOLUME_RATIO_MIN", "MAX_LAUNCH_ROWS", "WARMUP_MIN_PCT",
    "MODEL_VERSION", "evaluate_launch_radar", "record_launch_observations",
    "sealed_sector_counts", "track_velocity",
]
