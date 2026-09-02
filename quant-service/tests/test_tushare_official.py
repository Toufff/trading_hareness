from __future__ import annotations

import unittest
from datetime import date
from unittest import mock

from app import tushare_official
from app.tushare_official import default_probe_params


class DefaultProbeParamsClockTests(unittest.TestCase):
    def test_uses_shanghai_calendar_date_not_container_clock(self):
        # A container clock running in UTC must not leak into the probe's
        # default trade_date; the probe must ask the shared cn_today() helper
        # rather than call date.today() directly.
        with mock.patch.object(tushare_official, "cn_today", return_value=date(2026, 9, 1)):
            params = default_probe_params("bak_basic")
        self.assertEqual(params["trade_date"], "20260901")

    def test_explicit_as_of_still_overrides_the_clock(self):
        with mock.patch.object(tushare_official, "cn_today", return_value=date(2099, 1, 1)):
            params = default_probe_params("bak_basic", as_of=date(2026, 9, 1))
        self.assertEqual(params["trade_date"], "20260901")


if __name__ == "__main__":
    unittest.main()
