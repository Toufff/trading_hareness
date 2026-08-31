#!/usr/bin/env bash
# Encrypted off-site backup of the research database to Baidu Pan.
#
# The 28GB research database is the only copy of the full history: the edge
# prunes to stay bounded, so anything older than its window exists here alone.
# Pan is third-party storage, so the dump is encrypted before it leaves the box.
set -euo pipefail

REPO="${QUANT_REPO_ROOT:-$HOME/codebase/n8n}"
COMPOSE="/opt/homebrew/bin/docker compose -f ${REPO}/compose.yaml"
PASS_FILE="$HOME/.config/feishu-relay/pgbackup-passphrase.txt"
STAGE="$HOME/marketdata/tmp/pgbackup"
STAMP="$(TZ=Asia/Shanghai date +%Y%m%d)"
CHUNK_MB=1024

[[ -r "$PASS_FILE" ]] || { echo "passphrase missing: $PASS_FILE" >&2; exit 1; }
mkdir -p "$STAGE"
rm -f "$STAGE"/dump-*.part

echo "[1/3] pg_dump -> zstd -> gpg -> 1GB chunks"
started=$(date +%s)
$COMPOSE exec -T postgres pg_dump -U n8n -d n8n -Fc -Z 0 \
  | zstd -3 -T0 -q \
  | gpg --batch --yes --symmetric --cipher-algo AES256 \
        --passphrase-file "$PASS_FILE" --pinentry-mode loopback \
  | split -b "${CHUNK_MB}m" - "$STAGE/dump-"
elapsed=$(( $(date +%s) - started ))

# split names its output dump-aa, dump-ab ...; give them a stable suffix.
for f in "$STAGE"/dump-??; do [[ -e "$f" ]] && mv "$f" "$f.part"; done
total=$(du -ch "$STAGE"/dump-*.part 2>/dev/null | tail -1 | cut -f1)
count=$(ls -1 "$STAGE"/dump-*.part 2>/dev/null | wc -l | tr -d ' ')
echo "    dumped ${total} in ${count} chunk(s), ${elapsed}s"

echo "[2/3] sha256 manifest"
( cd "$STAGE" && shasum -a 256 dump-*.part > "manifest-${STAMP}.txt" )
echo "    $(wc -l < "$STAGE/manifest-${STAMP}.txt" | tr -d ' ') entries"

echo "[3/3] upload to pan"
"$HOME/.venvs/svc/bin/python" - "$STAGE" "$STAMP" <<'PY'
import os, sys, time
sys.path.insert(0, os.path.expanduser('~/codebase/n8n/scripts/marketdata'))
import pan_client as pc
stage, stamp = sys.argv[1], sys.argv[2]
base = f'/apps/股票paper存储/db-backups/research-pg/{stamp}'
files = sorted(f for f in os.listdir(stage) if f.startswith('dump-') or f.startswith('manifest-'))
for name in files:
    local = os.path.join(stage, name)
    t = time.time()
    pc.upload(local, f'{base}/{name}')
    print(f'    {name}  {os.path.getsize(local)/1024/1024:.0f}MB  {time.time()-t:.0f}s')
print(f'    -> {base}')
PY

echo "done. keep $PASS_FILE somewhere else too: without it the backup is unreadable."
