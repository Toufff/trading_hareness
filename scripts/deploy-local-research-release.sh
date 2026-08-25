#!/usr/bin/env bash
# Rebuild the workstation research and relay surfaces with auditable metadata.
# The edge remains the only live-polling writer; this never enables local relay
# pollers or any intraday_edge runtime loops.
set -euo pipefail

usage() {
  echo "usage: $0 <git-sha-or-tag> <release-label> [--apply]" >&2
  exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage
release_ref="$1"
release_label="$2"
apply=false
[[ "${3:-}" == "--apply" ]] && apply=true
[[ "${3:-}" == "" || "${3:-}" == "--apply" ]] || usage
[[ "$release_label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "release label contains unsupported characters" >&2; exit 2; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_sha="$(git -C "$repo_root" rev-parse --verify "${release_ref}^{commit}")"
built_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
release_paths=(quant-service feishu-adapter frontend compose.yaml scripts/audit-fuyao-capabilities.py scripts/deploy-local-research-release.sh)
github_branch="${QUANT_RELEASE_GITHUB_BRANCH:-main}"
[[ "$github_branch" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid GitHub branch" >&2; exit 2; }

# A user's unrelated worktree edit must not block an immutable release, but no
# file incorporated into either local image may differ from the supplied SHA.
git -C "$repo_root" diff --quiet "$release_sha" -- "${release_paths[@]}" || {
  echo "refusing to build a release whose source differs from ${release_sha}" >&2
  exit 1
}
git -C "$repo_root" diff --cached --quiet -- "${release_paths[@]}" || {
  echo "refusing to build staged local release source" >&2
  exit 1
}
github_sha="$(git -C "$repo_root" ls-remote --exit-code origin "refs/heads/$github_branch" | awk 'NR == 1 {print $1}')"
[[ "$github_sha" == "$release_sha" ]] || {
  echo "refusing local deploy: origin/$github_branch is $github_sha, requested release is $release_sha" >&2
  exit 1
}

printf 'release_sha=%s\nrelease_label=%s\ngithub_branch=%s\nbuilt_at=%s\n' "$release_sha" "$release_label" "$github_branch" "$built_at"
if [[ "$apply" != true ]]; then
  echo "dry run only; append --apply after the revision is committed"
  exit 0
fi

cd "$repo_root/frontend"
npm run build
cd "$repo_root"

env APP_GIT_SHA="$release_sha" APP_RELEASE="$release_label" APP_BUILD_CREATED_AT="$built_at" \
  docker compose build quant-research feishu-adapter
env APP_GIT_SHA="$release_sha" APP_RELEASE="$release_label" APP_BUILD_CREATED_AT="$built_at" \
  docker compose up -d --no-deps quant-research feishu-adapter

for attempt in {1..30}; do
  curl -fsS http://127.0.0.1:5681/health >/tmp/quant-local-release-health.json \
    && curl -fsS http://127.0.0.1:5680/health >/tmp/adapter-local-release-health.json && break
  sleep 2
done
test -s /tmp/quant-local-release-health.json
test -s /tmp/adapter-local-release-health.json
node scripts/verify-release-provenance.mjs --url http://127.0.0.1:5681/health --sha "$release_sha" --release "$release_label"
node scripts/verify-release-provenance.mjs --url http://127.0.0.1:5680/health --sha "$release_sha" --release "$release_label"
echo "local research release applied: ${release_label} (${release_sha})"
