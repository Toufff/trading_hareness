#!/usr/bin/env bash
# One-way watchlist sync: workstation (source of truth) -> 47 edge (scanner).
#
# The intraday scanner runs on the edge, but pool curation happens here on the
# workstation. These grew apart once (edge stuck on an 2026-08-16 snapshot while
# the workstation moved on), which left the scanner watching a dead pool, so
# this pushes the difference on a schedule instead of trusting a manual copy.
#
# Only differing rows are PUT: every PUT triggers a 45-day factor hydration on
# the edge, so a blind full-sync would burn provider quota for nothing. The
# write key stays on the edge; the local pool travels as base64 in the ssh
# environment because stdin already carries the remote script itself.
set -uo pipefail

EDGE="${QUANT_EDGE_HOST:-root@47.114.113.152}"
EDGE_KEY_FILE="${QUANT_EDGE_SSH_KEY:-$HOME/.ssh/feishu_relay_edge_ed25519}"
LOCAL_API="${QUANT_API_BASE:-http://127.0.0.1:5681}"

local_json="$(curl -sS -m 30 "$LOCAL_API/api/v1/intraday/watchlists" 2>/dev/null)"
if [[ -z "$local_json" ]] || ! printf '%s' "$local_json" | grep -q '"items"'; then
  echo "[watchlist-sync] local API unavailable; skip" >&2; exit 0
fi
WL_B64="$(printf '%s' "$local_json" | base64 | tr -d '\n')"

/usr/bin/ssh -i "$EDGE_KEY_FILE" -o BatchMode=yes -o IdentitiesOnly=yes \
    -o ConnectTimeout=20 "$EDGE" "WL_B64='$WL_B64' bash -s" <<'REMOTE'
set -uo pipefail
PY=/opt/quant-intraday-edge/.venv/bin/python
[ -x "$PY" ] || PY=python3
KEY="$(grep -E '^QUANT_WRITE_API_KEY=' /etc/quant-intraday-edge.env | cut -d= -f2-)"
export EDGE_WRITE_KEY="$KEY"
"$PY" - <<'PYEOF'
import base64, json, os, urllib.request

FIELDS = ("label", "enabled", "alert_on_entry", "alert_on_exit", "entry_price",
          "available_quantity", "hard_stop", "take_profit", "metadata")
API = "http://127.0.0.1:18110"
KEY = os.environ["EDGE_WRITE_KEY"]

local_items = {i["symbol"]: i for i in
               json.loads(base64.b64decode(os.environ["WL_B64"])).get("items", [])}
with urllib.request.urlopen(f"{API}/api/v1/intraday/watchlists", timeout=30) as r:
    edge_items = {i["symbol"]: i for i in json.load(r).get("items", [])}

def view(item):
    return {k: item.get(k) for k in FIELDS}

pushed = skipped = failed = 0
for symbol, item in sorted(local_items.items()):
    if symbol in edge_items and view(edge_items[symbol]) == view(item):
        skipped += 1
        continue
    body = {"symbol": symbol, **view(item)}
    req = urllib.request.Request(
        f"{API}/api/v1/intraday/watchlists/{symbol}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "X-Quant-Write-Key": KEY},
        method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        pushed += 1
        print(f"  pushed {symbol} ({item.get('label','')})")
    except Exception as error:
        failed += 1
        print(f"  FAILED {symbol}: {str(error)[:80]}")

# Symbols only on the edge are reported, never deleted: removal is a human call.
only_edge = sorted(set(edge_items) - set(local_items))
if only_edge:
    print(f"  edge-only (left untouched): {', '.join(only_edge)}")
print(f"  sync done: {pushed} pushed, {skipped} unchanged, {failed} failed, "
      f"{len(only_edge)} edge-only")
PYEOF
REMOTE
