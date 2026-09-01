from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.http_clients import public_http_client_status, public_proxy_url


class PublicProxyConfigurationTests(unittest.TestCase):
    def test_only_explicit_scoped_proxy_is_used(self) -> None:
        with patch.dict(os.environ, {"HTTP_PROXY": "http://implicit:1"}, clear=True):
            self.assertIsNone(public_proxy_url())
        with patch.dict(os.environ, {"QUANT_PUBLIC_HTTP_PROXY": "http://127.0.0.1:4537"}, clear=True):
            self.assertEqual(public_proxy_url(), "http://127.0.0.1:4537")
            self.assertTrue(public_http_client_status()["proxy_configured"])


if __name__ == "__main__":
    unittest.main()
