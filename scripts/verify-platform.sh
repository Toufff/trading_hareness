#!/usr/bin/env bash
set -euo pipefail

# One bounded, reproducible verification command for humans and maintenance
# agents. It does not mutate market data or call external providers.
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

docker compose exec -T quant-research python -m unittest discover -s tests -q
node --test feishu-adapter/*.test.mjs
(cd frontend && npm run typecheck && npm run build)
git diff --check

if command -v curl >/dev/null 2>&1; then
  curl -fsS http://127.0.0.1:5681/health >/tmp/quant-health.json
  curl -fsS http://127.0.0.1:5680/api/research/agent/context >/tmp/agent-context.json
  grep -q 'research_only_no_orders' /tmp/agent-context.json
  node scripts/verify-api-contract.mjs
  node scripts/generate-api-types.mjs --check
  rm -f /tmp/quant-health.json /tmp/agent-context.json
fi

echo "platform verification passed"
