#!/usr/bin/env bash
set -euo pipefail

# Archive a local file without copying Baidu credentials to the workstation.
# Data is split into bounded temporary pieces locally, streamed over SSH to the
# already-authorized edge adapter, and removed only after the remote uploader
# accepts each piece. The source file is never modified or deleted.

usage() {
  echo "usage: $0 --source FILE --remote-dir PATH [--ssh-host root@host] [--ssh-key FILE] [--part-bytes BYTES]" >&2
  exit 2
}

source_file=""
remote_dir=""
ssh_host="root@47.114.113.152"
ssh_key="${HOME}/.ssh/feishu_relay_edge_ed25519"
part_bytes="419430400"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) source_file="${2:-}"; shift 2 ;;
    --remote-dir) remote_dir="${2:-}"; shift 2 ;;
    --ssh-host) ssh_host="${2:-}"; shift 2 ;;
    --ssh-key) ssh_key="${2:-}"; shift 2 ;;
    --part-bytes) part_bytes="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ -n "$source_file" && -f "$source_file" ]] || { echo "source file does not exist" >&2; exit 2; }
[[ -n "$remote_dir" && "$remote_dir" == /* && "$remote_dir" != *".."* ]] || { echo "remote-dir must be an absolute safe path" >&2; exit 2; }
[[ "$part_bytes" =~ ^[0-9]+$ ]] && (( part_bytes >= 1048576 && part_bytes <= 471859200 )) || {
  echo "part-bytes must be between 1048576 and 471859200" >&2; exit 2;
}
[[ -r "$ssh_key" ]] || { echo "ssh key is not readable" >&2; exit 2; }

source_size="$(stat -f '%z' "$source_file" 2>/dev/null || stat -c '%s' "$source_file")"
source_sha256="$(shasum -a 256 "$source_file" | awk '{print $1}' 2>/dev/null || sha256sum "$source_file" | awk '{print $1}')"
base_name="$(basename "$source_file")"
temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/baidu-pan-archive.XXXXXX")"
cleanup() { rm -rf "$temp_dir"; }
trap cleanup EXIT INT TERM

remote_dir="${remote_dir%/}"
split -b "$part_bytes" -a 6 "$source_file" "$temp_dir/part-"

part_index=0
part_records=()
for part in "$temp_dir"/part-*; do
  [[ -f "$part" ]] || continue
  part_index=$((part_index + 1))
  filename="part-$(printf '%06d' "$part_index").bin"
  remote_path="${remote_dir}/${filename}"
  part_size="$(stat -f '%z' "$part" 2>/dev/null || stat -c '%s' "$part")"
  part_sha256="$(shasum -a 256 "$part" | awk '{print $1}' 2>/dev/null || sha256sum "$part" | awk '{print $1}')"
  echo "uploading ${filename} bytes=${part_size}" >&2
  result="$(ssh -i "$ssh_key" -o BatchMode=yes "$ssh_host" \
    "docker exec -i feishu-relay-edge-adapter node /app/scripts/baidu-pan-upload-file.mjs - '$remote_path'" < "$part")"
  printf '%s\n' "$result" | grep -q '"path"' || { echo "remote uploader returned no path" >&2; exit 1; }
  part_records+=("{\"filename\":\"${filename}\",\"bytes\":${part_size},\"sha256\":\"${part_sha256}\"}")
  rm -f "$part"
done

manifest_file="$temp_dir/manifest.json"
printf '{"schema":"local-file-archive-v1","source_filename":"%s","source_bytes":%s,"source_sha256":"%s","part_bytes":%s,"parts":[%s],"restore_policy":"concatenate_parts_in_order","research_only":true,"live_effect":"none"}\n' \
  "${base_name//\\/\\\\}" "$source_size" "$source_sha256" "$part_bytes" "$(IFS=,; echo "${part_records[*]}")" > "$manifest_file"
manifest_path="${remote_dir}/manifest.json"
ssh -i "$ssh_key" -o BatchMode=yes "$ssh_host" \
  "docker exec -i feishu-relay-edge-adapter node /app/scripts/baidu-pan-upload-file.mjs - '$manifest_path'" < "$manifest_file"
echo "archive_complete remote_dir=${remote_dir} parts=${part_index} source_bytes=${source_size} source_sha256=${source_sha256}" >&2
