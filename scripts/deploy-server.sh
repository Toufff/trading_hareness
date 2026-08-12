#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: DEPLOY_ENV_FILE=/path/to/server.env $0 user@server /opt/market-relay" >&2
  exit 64
fi

deploy_host="$1"
deploy_dir="$2"
deploy_root="$(cd "$(dirname "$0")/.." && pwd)"

if [[ -n "${DEPLOY_ENV_FILE:-}" && ! -f "$DEPLOY_ENV_FILE" ]]; then
  echo "DEPLOY_ENV_FILE does not exist: $DEPLOY_ENV_FILE" >&2
  exit 66
fi

ssh "$deploy_host" "mkdir -p '$deploy_dir'"
if [[ -n "${DEPLOY_ENV_FILE:-}" ]]; then
  scp "$DEPLOY_ENV_FILE" "$deploy_host:$deploy_dir/.env"
elif ! ssh "$deploy_host" "test -f '$deploy_dir/.env'"; then
  echo "remote .env is missing; set DEPLOY_ENV_FILE to a completed deploy/.env.server.example copy" >&2
  exit 65
fi

rsync -az \
  --exclude '.env' --exclude 'backups/' --exclude 'logs/' --exclude 'state/' \
  --exclude 'node_modules/' --exclude '__pycache__/' --exclude '.DS_Store' \
  "$deploy_root/" "$deploy_host:$deploy_dir/"

ssh "$deploy_host" "cd '$deploy_dir' && docker compose --env-file .env -f deploy/compose.server.yaml config -q && docker compose --env-file .env -f deploy/compose.server.yaml up -d --build"
echo "deployment complete; inspect with: ssh $deploy_host 'cd $deploy_dir && docker compose --env-file .env -f deploy/compose.server.yaml ps'"
