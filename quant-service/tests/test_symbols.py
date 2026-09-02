from __future__ import annotations

import unittest

from app.symbols import canonical_symbol


class CanonicalSymbolTests(unittest.TestCase):
    def test_sh_prefixed_index_is_not_misrouted_to_shenzhen(self):
        # The historical bug: taking the trailing 6 digits and routing a "0"
        # prefix to SZ turned CSI 300 into a nonexistent Shenzhen stock.
        self.assertEqual(canonical_symbol("sh000300", kind="index"), "000300.SH")
        self.assertEqual(canonical_symbol("sh000300"), "000300.SH")

    def test_suffix_form_is_normalized_case_insensitively(self):
        self.assertEqual(canonical_symbol("600000.sh"), "600000.SH")
        self.assertEqual(canonical_symbol("000001.sz"), "000001.SZ")

    def test_prefix_form_is_normalized(self):
        self.assertEqual(canonical_symbol("SZ000001"), "000001.SZ")
        self.assertEqual(canonical_symbol("bj430047"), "430047.BJ")

    def test_bare_mainboard_and_registration_board_codes(self):
        self.assertEqual(canonical_symbol("600519"), "600519.SH")
        self.assertEqual(canonical_symbol("000001"), "000001.SZ")
        self.assertEqual(canonical_symbol("300750"), "300750.SZ")
        self.assertEqual(canonical_symbol("688981"), "688981.SH")

    def test_shanghai_b_share_900xxx_is_not_confused_with_beijing(self):
        self.assertEqual(canonical_symbol("900901"), "900901.SH")

    def test_shenzhen_b_share_200xxx(self):
        self.assertEqual(canonical_symbol("200011"), "200011.SZ")

    def test_beijing_new_format_920xxx_is_not_confused_with_shanghai_b_share(self):
        self.assertEqual(canonical_symbol("920819"), "920819.BJ")

    def test_beijing_legacy_neeq_430xxx(self):
        self.assertEqual(canonical_symbol("430047"), "430047.BJ")

    def test_index_kind_resolves_000_and_399_prefixes(self):
        self.assertEqual(canonical_symbol("000300", kind="index"), "000300.SH")
        self.assertEqual(canonical_symbol("000001", kind="index"), "000001.SH")
        self.assertEqual(canonical_symbol("000688", kind="index"), "000688.SH")
        self.assertEqual(canonical_symbol("399006", kind="index"), "399006.SZ")

    def test_any_kind_falls_back_across_index_and_stock_tables(self):
        # 900901 has no index mapping, so "any" must fall through to the
        # stock table rather than returning None.
        self.assertEqual(canonical_symbol("900901", kind="any"), "900901.SH")

    def test_unrecognized_input_returns_none(self):
        self.assertIsNone(canonical_symbol("not-a-symbol"))
        self.assertIsNone(canonical_symbol(""))
        self.assertIsNone(canonical_symbol(None))
        self.assertIsNone(canonical_symbol("12345"))


if __name__ == "__main__":
    unittest.main()
