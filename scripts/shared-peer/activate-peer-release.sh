#!/usr/bin/env bash
set -euo pipefail

repo_archive="${1:?repo archive is required}"
wheelhouse_archive="${2:?wheelhouse archive is required}"
peer_home="${PEER_HOME:-/home/stockpeer}"
release_id="${RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
release_store="${PEER_RELEASES_ROOT:-${peer_home}/.local/share/trading-hareness/releases}"
release_root="${release_store}/${release_id}"
repo_target="${release_root}/trading_hareness"
wheelhouse_target="${release_root}/wheelhouse"
current_repo="${peer_home}/trading_hareness"
current_wheelhouse="${peer_home}/wheelhouse"
saved_env="$(mktemp)"
trap 'rm -f "${saved_env}"' EXIT

test -r "${repo_archive}"
test -r "${wheelhouse_archive}"
tar -tf "${repo_archive}" >/dev/null
tar -tf "${wheelhouse_archive}" >/dev/null

if [[ -f "${current_repo}/deploy/shared-peer/.env" ]]; then
  install -m 0600 "${current_repo}/deploy/shared-peer/.env" "${saved_env}"
fi

install -d -m 0755 "${release_store}" "${release_root}"
tar -xf "${repo_archive}" -C "${release_root}"
tar -xf "${wheelhouse_archive}" -C "${release_root}"
test -f "${repo_target}/deploy/shared-peer/compose.yaml"
test -f "${wheelhouse_target}/SHA256SUMS"
(
  cd "${wheelhouse_target}"
  tr -d '\r' < SHA256SUMS | sha256sum --check - >/dev/null
)

if [[ -s "${saved_env}" ]]; then
  install -m 0600 "${saved_env}" "${repo_target}/deploy/shared-peer/.env"
fi
# Environment bundles are commonly produced on the Windows owner host. Strip
# CRLF before Linux shells source the file; otherwise a trailing CR can become
# part of an HTTP header value and make an otherwise valid static API key fail.
sed -i 's/\r$//' "${repo_target}/deploy/shared-peer/.env"
grep -q '^PEER_WHEELHOUSE_PATH=' "${repo_target}/deploy/shared-peer/.env" || \
  printf '\nPEER_WHEELHOUSE_PATH=%s\n' "${current_wheelhouse}" >> "${repo_target}/deploy/shared-peer/.env"

if [[ "$(id -u)" -eq 0 ]]; then
  chown -R stockpeer:stockpeer "${release_root}"
fi
if [[ -e "${current_repo}" && ! -L "${current_repo}" ]]; then
  mv "${current_repo}" "${release_root}/previous-working-tree"
fi
ln -sfn "${repo_target}" "${current_repo}.next"
mv -Tf "${current_repo}.next" "${current_repo}"
if [[ -e "${current_wheelhouse}" && ! -L "${current_wheelhouse}" ]]; then
  mv "${current_wheelhouse}" "${release_root}/previous-wheelhouse"
fi
ln -sfn "${wheelhouse_target}" "${current_wheelhouse}.next"
mv -Tf "${current_wheelhouse}.next" "${current_wheelhouse}"

printf 'release=%s\nrepo=%s\nwheelhouse=%s\n' "${release_id}" "${repo_target}" "${wheelhouse_target}"
