"""Verify the documented custom-bot signing/request contract without network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json


def signature(timestamp: int, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def main() -> None:
    timestamp = 1_599_360_473
    # Same timestamp/secret pair used by the official Feishu guide. Keeping a
    # fixed expected digest prevents a test that merely compares the function
    # with itself from accepting a wrong algorithm.
    secret = "demo"
    payload = {
        "timestamp": str(timestamp),
        "sign": signature(timestamp, secret),
        "msg_type": "text",
        "content": {"text": "mock discipline line triggered"},
    }
    assert payload["sign"] == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="
    assert payload["msg_type"] == "text"
    assert payload["content"]["text"].startswith("mock")
    print(json.dumps({"status": "passed", "network": "mocked", "transport": "custom_bot"}, sort_keys=True))


if __name__ == "__main__":
    main()
