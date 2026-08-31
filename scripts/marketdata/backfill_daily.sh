#!/usr/bin/env bash
# Backfill historical trading days through the existing sync endpoint.
#
# One request per trading day, several days in flight at once. The endpoint
# already walks its own provider candidate list per API, so parallelism here is
# across dates rather than across sources -- that keeps each request's fallback
# behaviour intact while still using more than one upstream at a time.
#
# A day that already has rows is skipped, so the script is safe to re-run and
# resumes where it stopped.
set -uo pipefail

REPO="${QUANT_REPO_ROOT:-$HOME/codebase/n8n}"
API="${QUANT_API_BASE:-http://127.0.0.1:5681}"
KEY="$(grep -E '^QUANT_WRITE_API_KEY=' "$REPO/.env" | cut -d= -f2-)"
START="${1:?usage: backfill_daily.sh <start YYYY-MM-DD> <end YYYY-MM-DD> [parallel] [api_probe]}"
END="${2:?}"
PAR="${3:-3}"
PROBE="${4:-moneyflow}"
STATE="$HOME/marketdata/tmp/backfill-daily.state"
LOG="$HOME/marketdata/tmp/backfill-daily.log"
mkdir -p "$(dirname "$STATE")"

[[ -n "$KEY" ]] || { echo "missing QUANT_WRITE_API_KEY" >&2; exit 1; }

# Trading days come from the exchange calendar already in the database, so a
# weekend or holiday is never requested.
days="$(/opt/homebrew/bin/docker compose -f "$REPO/compose.yaml" exec -T postgres \
  psql -U n8n -d n8n -tAc "
    SELECT DISTINCT trading_date::text FROM quant.canonical_bars_daily
    WHERE trading_date BETWEEN DATE '$START' AND DATE '$END'
    ORDER BY 1" 2>/dev/null)"
total="$(printf '%s\n' "$days" | grep -c .)"
echo "$(date '+%H:%M:%S') backfill $START..$END : $total trading days, parallel=$PAR, probe=$PROBE" | tee -a "$LOG"

one_day() {
  local d="$1"
  grep -qx "$d" "$STATE" 2>/dev/null && { echo "  $d already done"; return 0; }
  # Skip a date that already carries the probe API, so a re-run is cheap.
  local have
  have="$(/opt/homebrew/bin/docker compose -f "$REPO/compose.yaml" exec -T postgres \
    psql -U n8n -d n8n -tAc "SELECT count(*) FROM quant.tushare_raw_records
      WHERE api_name='$PROBE' AND available_at::date = DATE '$d'" 2>/dev/null | tr -d ' ')"
  if [[ "${have:-0}" -gt 0 ]]; then
    echo "$d" >> "$STATE"; echo "  $d already has $PROBE ($have rows), skipped"; return 0
  fi
  local t0 code
  t0=$(date +%s)
  code="$(curl -sS -m 1500 -o /dev/null -w '%{http_code}' -X POST "$API/api/v1/market/sync/tushare" \
      -H 'content-type: application/json' -H "X-Quant-Write-Key: $KEY" \
      -d "{\"trade_date\":\"$d\"}" 2>/dev/null)"
  if [[ "$code" == "200" ]]; then
    echo "$d" >> "$STATE"
    echo "  $d OK ($(( $(date +%s) - t0 ))s)"
  else
    echo "  $d FAILED http=$code ($(( $(date +%s) - t0 ))s)"
  fi
}
export -f one_day
export REPO API KEY STATE PROBE

printf '%s\n' "$days" | grep . | xargs -P "$PAR" -I{} bash -c 'one_day "$@"' _ {} 2>&1 | tee -a "$LOG"
echo "$(date '+%H:%M:%S') done: $(grep -c . "$STATE" 2>/dev/null || echo 0)/$total days recorded" | tee -a "$LOG"
