#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "run as the non-root stockpeer user" >&2
  exit 1
fi

ssh_host="${1:?lightServer SSH host is required}"
ssh_port="${2:-3535}"
ssh_dir="${HOME}/.ssh"
key_path="${ssh_dir}/peer_tunnel_ed25519"
known_hosts="${ssh_dir}/known_hosts"

install -d -m 0700 "${ssh_dir}"
if [[ ! -f "${key_path}" ]]; then
  ssh-keygen -q -t ed25519 -N '' -f "${key_path}" -C "stockpeer-self-tunnel"
fi
public_key="$(tr -d '\r\n' < "${key_path}.pub")"
touch "${ssh_dir}/authorized_keys" "${known_hosts}"
grep -qxF "${public_key}" "${ssh_dir}/authorized_keys" || printf '%s\n' "${public_key}" >> "${ssh_dir}/authorized_keys"

temporary="$(mktemp)"
trap 'rm -f "${temporary}"' EXIT
ssh-keyscan -p "${ssh_port}" "${ssh_host}" > "${temporary}" 2>/dev/null
test -s "${temporary}"
cat "${temporary}" >> "${known_hosts}"
sort -u -o "${known_hosts}" "${known_hosts}"
chmod 0600 "${key_path}" "${ssh_dir}/authorized_keys" "${known_hosts}"

ssh -p "${ssh_port}" -i "${key_path}" -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="${known_hosts}" \
  "$(id -un)@${ssh_host}" true

printf 'self_tunnel=ready\nkey=%s\nknown_hosts=%s\n' "${key_path}" "${known_hosts}"
