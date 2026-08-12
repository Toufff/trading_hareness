#!/bin/zsh
# Copy text in any desktop app, then run this script (or bind it in Shortcuts).
# The text is kept only in a one-time local draft; it is never placed in a URL.
set -euo pipefail

relay_text="$(/usr/bin/pbpaste)"
if [[ -z "${relay_text//[[:space:]]/}" ]]; then
  /usr/bin/osascript -e 'display notification "剪贴板没有可投递的文字" with title "市场复盘投递"'
  exit 1
fi

relay_payload="$(printf '%s' "$relay_text" | /Users/papa/codebase/.venv/bin/python -c 'import json, sys; print(json.dumps({"text": sys.stdin.read()}))')"
relay_reply="$(printf '%s' "$relay_payload" | /usr/bin/curl --fail --silent --show-error \
  --header 'content-type: application/json' --data-binary @- http://127.0.0.1:5680/relay-clipboard-draft)"
relay_draft_id="$(printf '%s' "$relay_reply" | /Users/papa/codebase/.venv/bin/python -c 'import json, sys; print(json.load(sys.stdin)["draft_id"])')"

/usr/bin/open "http://localhost:5680/relay?draft=${relay_draft_id}"
