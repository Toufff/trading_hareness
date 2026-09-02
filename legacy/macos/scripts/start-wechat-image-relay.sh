#!/bin/zsh

set -euo pipefail

export PATH=/Users/papa/codebase/.venv/bin:/usr/bin:/bin:/usr/sbin:/sbin

relay_tag="${1:-${WECHAT_RELAY_TAG:-xiaolan}}"
relay_root="/Users/papa/codebase/n8n"
relay_pidfile="${relay_root}/state/wechat-image-relay.pid"
relay_log="${relay_root}/logs/wechat-image-relay.log"
relay_args=()
relay_chat_dir_id="${WECHAT_RELAY_CHAT_DIR_ID:-71345daa03ac00d81e0f824bb580d85e}"

case "$relay_tag" in
	liwei|liuzi|xiaolan) ;;
	*)
		echo "Invalid route tag: ${relay_tag}; expected liwei, liuzi, or xiaolan" >&2
		exit 2
		;;
esac

mkdir -p "${relay_root}/state" "${relay_root}/logs"

if [[ -f "$relay_pidfile" ]]; then
	old_pid="$(cat "$relay_pidfile")"
	if [[ "$old_pid" =~ '^[0-9]+$' ]] && kill -0 "$old_pid" >/dev/null 2>&1; then
		echo "wechat-image-relay already running with pid ${old_pid}"
		exit 0
	fi
fi

if [[ "${WECHAT_RELAY_INCLUDE_RWTEMP:-0}" == "1" ]]; then
	relay_args+=(--include-rwtemp)
fi
if [[ -n "$relay_chat_dir_id" ]]; then
	relay_args+=(--chat-dir-id "$relay_chat_dir_id")
fi

nohup /Users/papa/codebase/.venv/bin/python \
	/Users/papa/codebase/n8n/scripts/wechat-image-relay.py \
	--tag "$relay_tag" \
	--source-label "${WECHAT_RELAY_SOURCE_LABEL:-微信小蓝炒股会媒体监控}" \
	"${relay_args[@]}" \
	>> "$relay_log" 2>&1 &

echo "$!" > "$relay_pidfile"
echo "started wechat-image-relay pid $(cat "$relay_pidfile") route #${relay_tag}"
echo "log: $relay_log"
