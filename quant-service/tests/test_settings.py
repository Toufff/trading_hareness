"""Tests for the centralized environment-variable settings snapshot."""

from __future__ import annotations

import unittest

from app.settings import Settings


class SettingsFromEnvironDefaultsTests(unittest.TestCase):
    def test_defaults_match_the_previous_inline_os_getenv_behaviour(self):
        settings = Settings.from_environ({})

        self.assertEqual(settings.quant_universe, ())
        self.assertIsNone(settings.dashboard_public_url)
        self.assertEqual(settings.shared_read_api_key, "")
        self.assertEqual(settings.write_api_key, "")
        self.assertFalse(settings.allow_unauthenticated_writes)
        self.assertEqual(settings.data_dir, "/var/lib/quant")
        self.assertFalse(settings.legacy_schema_bootstrap_enabled)
        self.assertTrue(settings.control_plane_writes_enabled)
        self.assertEqual(settings.provider_global_rate_limit_max_wait_seconds, 5.0)
        self.assertTrue(settings.intraday_minute_profile_capture_enabled)
        self.assertEqual(settings.intraday_minute_profile_retention_days, 90)
        self.assertEqual(settings.intraday_minute_profile_max_symbols, 40)
        self.assertEqual(settings.longhu_intraday_max_symbols, 24)
        self.assertTrue(settings.strategy_review_automation_enabled)
        self.assertTrue(settings.post_close_strategy_automation_enabled)
        self.assertTrue(settings.ten_day_leader_rotation_automation_enabled)
        self.assertTrue(settings.daily_summary_automation_enabled)
        self.assertTrue(settings.ths_concept_member_backfill_enabled)
        self.assertEqual(settings.ths_concept_member_backfill_batch_size, 25)
        self.assertTrue(settings.all_board_member_backfill_enabled)
        self.assertEqual(settings.all_board_member_backfill_batch_size, 10)
        self.assertFalse(settings.retention_maintenance_automation_enabled)
        self.assertTrue(settings.market_event_capture_enabled)
        self.assertTrue(settings.all_a_level1_capture_enabled)


class SettingsFromEnvironOverridesTests(unittest.TestCase):
    def test_quant_universe_splits_and_trims_comma_separated_symbols(self):
        settings = Settings.from_environ({"QUANT_UNIVERSE": " 600519.SH, 000001.SZ ,"})
        self.assertEqual(settings.quant_universe, ("600519.SH", "000001.SZ"))

    def test_dashboard_public_url_is_trimmed_and_trailing_slash_stripped(self):
        settings = Settings.from_environ({"QUANT_DASHBOARD_PUBLIC_URL": " https://x.example/ "})
        self.assertEqual(settings.dashboard_public_url, "https://x.example")

    def test_batch_sizes_clamp_to_their_historical_upper_bound_of_25(self):
        settings = Settings.from_environ({
            "THS_CONCEPT_MEMBER_BACKFILL_BATCH_SIZE": "999", "ALL_BOARD_MEMBER_BACKFILL_BATCH_SIZE": "999",
        })
        self.assertEqual(settings.ths_concept_member_backfill_batch_size, 25)
        self.assertEqual(settings.all_board_member_backfill_batch_size, 25)

    def test_invalid_integer_values_fall_back_to_the_default(self):
        settings = Settings.from_environ({"INTRADAY_MINUTE_PROFILE_RETENTION_DAYS": "not-a-number"})
        self.assertEqual(settings.intraday_minute_profile_retention_days, 90)

    def test_boolean_flags_accept_the_shared_vocabulary(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(Settings.from_environ({"QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED": value})
                             .retention_maintenance_automation_enabled)
        for value in ("0", "false", "no", "off", ""):
            self.assertFalse(Settings.from_environ({"QUANT_RETENTION_MAINTENANCE_AUTOMATION_ENABLED": value})
                              .retention_maintenance_automation_enabled)

    def test_rate_limit_wait_seconds_is_clamped_between_zero_and_thirty(self):
        self.assertEqual(
            Settings.from_environ({"QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS": "999"})
            .provider_global_rate_limit_max_wait_seconds, 30.0)
        self.assertEqual(
            Settings.from_environ({"QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS": "-5"})
            .provider_global_rate_limit_max_wait_seconds, 0.0)
        self.assertEqual(
            Settings.from_environ({"QUANT_PROVIDER_GLOBAL_RATE_LIMIT_MAX_WAIT_SECONDS": "not-a-number"})
            .provider_global_rate_limit_max_wait_seconds, 5.0)


if __name__ == "__main__":
    unittest.main()
