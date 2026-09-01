#!/usr/bin/env python3
"""Post-close watchlist refresh: admit today's proposals, retire the inactive.

The scanner is only as good as its pool, and the pool went stale once (frozen
on 2026-08-16 while the market rotated away). This closes the loop the strategy
registry already intends: limit_up_continuation proposes a dense universe and
disclosure_day_watch allocates scarce slots -- both were writing proposals that
nobody promoted.

Admission: top-ranked proposals from the latest as_of_date, capped per source.
Retirement: an auto-admitted name (label carries its source tag) that shows no
life for RETIRE_AFTER_SESSIONS straight sessions (no limit-up, never >5% up) is
disabled, not deleted. Hand-added names are never touched automatically.

Shanghai-time gate + daily marker: launchd fires on a plain interval because
this host runs US Pacific; the script decides whether the moment is right.
"""
from __future__ import annotations
import json, os, subprocess, sys, urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

API = os.environ.get("QUANT_API_BASE", "http://127.0.0.1:5681")
REPO = os.environ.get("QUANT_REPO_ROOT", os.path.expanduser("~/codebase/n8n"))
COMPOSE = ["/opt/homebrew/bin/docker", "compose", "-f", f"{REPO}/compose.yaml"]
STATE_DIR = os.path.expanduser("~/Library/Application Support/quant-watchlist-refresh")
ADMIT_CAPS = {"limit_up_continuation": 8, "disclosure_day_watch": 4}
SOURCE_TAGS = {"limit_up_continuation": "延续", "disclosure_day_watch": "披露"}
AUTO_TAGS = tuple(SOURCE_TAGS.values())
RETIRE_AFTER_SESSIONS = 5
WINDOW_OPEN, WINDOW_CLOSE = "1645", "2359"


def psql(sql: str) -> str:
    p = subprocess.run(COMPOSE + ["exec", "-T", "postgres", "psql", "-U", "n8n", "-d", "n8n",
                                  "-v", "ON_ERROR_STOP=1", "-P", "pager=off", "-tAc", sql],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode()[:200])
    return p.stdout.decode().strip()


def write_key() -> str:
    with open(f"{REPO}/.env") as f:
        for line in f:
            if line.startswith("QUANT_WRITE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("QUANT_WRITE_API_KEY missing")


def api_get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=30) as r:
        return json.load(r)


def api_put(path: str, body: dict, key: str):
    req = urllib.request.Request(f"{API}{path}", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          "X-Quant-Write-Key": key}, method="PUT")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main() -> int:
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    hhmm = now.strftime("%H%M")
    if not (WINDOW_OPEN <= hhmm <= WINDOW_CLOSE):
        return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    marker = os.path.join(STATE_DIR, f"done-{now:%Y-%m-%d}")
    if os.path.exists(marker):
        return 0

    latest = psql("SELECT max(as_of_date) FROM quant.strategy_watchlist_proposals "
                  "WHERE as_of_date <= CURRENT_DATE")
    if not latest:
        print("no proposals yet"); return 0

    pool = {i["symbol"]: i for i in api_get("/api/v1/intraday/watchlists").get("items", [])}
    key = write_key()
    tag_date = latest[5:].replace("-", "")
    admitted = retired = 0

    # --- admission ---
    for strategy, cap in ADMIT_CAPS.items():
        rows = psql(
            f"SELECT p.symbol || '|' || coalesce(i.name, p.symbol) "
            f"FROM quant.strategy_watchlist_proposals p "
            f"LEFT JOIN quant.instruments i USING (symbol) "
            f"WHERE p.as_of_date = DATE '{latest}' AND p.strategy_key = '{strategy}' "
            f"ORDER BY p.proposal_rank LIMIT {cap * 2}")
        taken = 0
        for row in rows.splitlines():
            if taken >= cap:
                break
            symbol, name = row.split("|", 1)
            if symbol in pool:
                continue
            label = f"{name}·{SOURCE_TAGS[strategy]}{tag_date}"
            try:
                api_put(f"/api/v1/intraday/watchlists/{symbol}",
                        {"symbol": symbol, "label": label, "available_quantity": 0,
                         "alert_on_entry": True, "alert_on_exit": True}, key)
                print(f"  admitted {symbol} {label}")
                admitted += 1; taken += 1
            except Exception as error:
                print(f"  FAILED admit {symbol}: {str(error)[:80]}")

    # --- retirement (auto-admitted, no life for N sessions) ---
    for symbol, item in pool.items():
        label = item.get("label") or ""
        if not item.get("enabled") or not any(t in label for t in AUTO_TAGS):
            continue
        alive = psql(
            f"SELECT count(*) FROM (SELECT close, limit_up, pre_close FROM quant.canonical_bars_daily "
            f"WHERE symbol = '{symbol}' ORDER BY trading_date DESC LIMIT {RETIRE_AFTER_SESSIONS}) b "
            f"WHERE b.close >= b.limit_up OR (b.close / b.pre_close - 1) > 0.05")
        sessions = psql(
            f"SELECT count(*) FROM (SELECT 1 FROM quant.canonical_bars_daily "
            f"WHERE symbol = '{symbol}' ORDER BY trading_date DESC LIMIT {RETIRE_AFTER_SESSIONS}) s")
        if int(sessions or 0) >= RETIRE_AFTER_SESSIONS and int(alive or 0) == 0:
            try:
                api_put(f"/api/v1/intraday/watchlists/{symbol}",
                        {"symbol": symbol, "label": label, "enabled": False,
                         "available_quantity": item.get("available_quantity", 0),
                         "alert_on_entry": item.get("alert_on_entry", True),
                         "alert_on_exit": item.get("alert_on_exit", True)}, key)
                print(f"  retired {symbol} {label} (quiet {RETIRE_AFTER_SESSIONS} sessions)")
                retired += 1
            except Exception as error:
                print(f"  FAILED retire {symbol}: {str(error)[:80]}")

    # --- adaptive anomaly thresholds (per-symbol rolling percentiles) ---
    # The volume_anomaly rule reads volume_anomaly_thresholds from watchlist
    # metadata and takes max(absolute floor, percentile), so publishing a
    # symbol's own P95/P90 here is what restores discrimination on chronically
    # active names. Sparse history publishes nothing and the rule keeps its
    # absolute floors (fail-closed).
    refreshed = 0
    for symbol, item in pool.items():
        if not item.get("enabled"):
            continue
        row = psql(
            f"SELECT round(percentile_cont(0.95) WITHIN GROUP (ORDER BY volume_ratio)::numeric, 2) || '|' || "
            f"round(percentile_cont(0.90) WITHIN GROUP (ORDER BY turnover_rate)::numeric, 2) || '|' || "
            f"count(DISTINCT observed_at::date) || '|' || count(*) "
            f"FROM quant.intraday_quote_observations "
            f"WHERE symbol = '{symbol}' AND volume_ratio IS NOT NULL AND turnover_rate IS NOT NULL "
            f"AND observed_at >= CURRENT_DATE - 30")
        if not row:
            continue
        p95, p90, days, samples = row.split("|")
        if int(days) < 8 or int(samples) < 1500:
            continue
        current = (item.get("metadata") or {}).get("volume_anomaly_thresholds") or {}
        want = {"volume_ratio_p95": float(p95), "turnover_rate_p90": float(p90),
                "window_days": int(days), "samples": int(samples),
                "computed_on": str(date.today())}
        prev_p95 = current.get("volume_ratio_p95")
        prev_p90 = current.get("turnover_rate_p90")
        close_enough = (prev_p95 is not None and prev_p90 is not None
                        and abs(float(prev_p95) - want["volume_ratio_p95"]) < 0.05 * max(1.0, float(prev_p95))
                        and abs(float(prev_p90) - want["turnover_rate_p90"]) < 0.05 * max(1.0, float(prev_p90)))
        if close_enough:
            continue
        metadata = dict(item.get("metadata") or {})
        metadata["volume_anomaly_thresholds"] = want
        try:
            api_put(f"/api/v1/intraday/watchlists/{symbol}",
                    {"symbol": symbol, "label": item.get("label") or symbol,
                     "enabled": True, "metadata": metadata,
                     "available_quantity": item.get("available_quantity", 0),
                     "alert_on_entry": item.get("alert_on_entry", True),
                     "alert_on_exit": item.get("alert_on_exit", True),
                     "entry_price": item.get("entry_price"),
                     "hard_stop": item.get("hard_stop"),
                     "take_profit": item.get("take_profit")}, key)
            refreshed += 1
            print(f"  thresholds {symbol}: vr_p95={p95} to_p90={p90} ({days}d/{samples} obs)")
        except Exception as error:
            print(f"  FAILED thresholds {symbol}: {str(error)[:80]}")

    with open(marker, "w") as f:
        f.write(f"{admitted} admitted, {retired} retired, {refreshed} thresholds\n")
    print(f"refresh done ({latest}): {admitted} admitted, {retired} retired, {refreshed} thresholds")

    # push the new pool to the edge right away
    subprocess.run(["bash", f"{REPO}/scripts/sync-watchlist-to-edge.sh"], timeout=600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
