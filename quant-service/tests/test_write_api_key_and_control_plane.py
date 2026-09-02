"""Focused coverage for the startup write-key gate and control-plane toggle.

Kept separate from the large ``test_platform_boundaries.py`` module (which is
bounded by ``scripts/verify_architecture.py``'s oversized-test-module guard)
rather than folded in there.
"""

from __future__ import annotations

import os
import unittest

from app.main import (
    _write_api_key_state,
    configured_write_api_key,
    control_plane_writes_enabled,
    resolve_write_api_key,
    write_access_allowed,
)


class WriteAccessNonAsciiTests(unittest.TestCase):
    def test_write_access_rejects_non_ascii_keys_instead_of_raising(self):
        # ``secrets.compare_digest`` raises TypeError on non-ASCII str
        # arguments, which used to surface as an unhandled 500 instead of a
        # normal 401 rejection.
        self.assertFalse(write_access_allowed("POST", "错误密钥", "configured"))
        self.assertTrue(write_access_allowed("POST", "配置的密钥", "配置的密钥"))


class ResolveWriteApiKeyTests(unittest.TestCase):
    def test_resolve_write_api_key_fails_closed_without_a_key_or_explicit_opt_out(self):
        previous_key = os.environ.get("QUANT_WRITE_API_KEY")
        previous_opt_out = os.environ.get("QUANT_ALLOW_UNAUTHENTICATED_WRITES")
        previous_state = configured_write_api_key()
        try:
            os.environ.pop("QUANT_WRITE_API_KEY", None)
            os.environ.pop("QUANT_ALLOW_UNAUTHENTICATED_WRITES", None)
            with self.assertRaisesRegex(RuntimeError, "QUANT_WRITE_API_KEY is required"):
                resolve_write_api_key()

            os.environ["QUANT_ALLOW_UNAUTHENTICATED_WRITES"] = "1"
            resolve_write_api_key()
            self.assertEqual(configured_write_api_key(), "")

            os.environ.pop("QUANT_ALLOW_UNAUTHENTICATED_WRITES", None)
            os.environ["QUANT_WRITE_API_KEY"] = "  a-real-key  "
            resolve_write_api_key()
            self.assertEqual(configured_write_api_key(), "a-real-key")
        finally:
            if previous_key is None:
                os.environ.pop("QUANT_WRITE_API_KEY", None)
            else:
                os.environ["QUANT_WRITE_API_KEY"] = previous_key
            if previous_opt_out is None:
                os.environ.pop("QUANT_ALLOW_UNAUTHENTICATED_WRITES", None)
            else:
                os.environ["QUANT_ALLOW_UNAUTHENTICATED_WRITES"] = previous_opt_out
            _write_api_key_state["value"] = previous_state


class ControlPlaneWritesEnabledTests(unittest.TestCase):
    def test_control_plane_writes_enabled_defaults_true_and_honors_the_peer_override(self):
        self.assertTrue(control_plane_writes_enabled({}))
        self.assertTrue(control_plane_writes_enabled({"QUANT_CONTROL_PLANE_WRITES_ENABLED": "true"}))
        self.assertFalse(control_plane_writes_enabled({"QUANT_CONTROL_PLANE_WRITES_ENABLED": "false"}))
        self.assertFalse(control_plane_writes_enabled({"QUANT_CONTROL_PLANE_WRITES_ENABLED": "0"}))


if __name__ == "__main__":
    unittest.main()
