"""core/numbers_cn.py 分支补充测试：中文数字、金额打分与格式化边界。"""
import unittest

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.numbers_cn import _cn_int, cn_to_number, fmt_int, fmt_yuan, parse_amount


class CnIntTest(unittest.TestCase):
    def test_digits_only(self):
        self.assertEqual(_cn_int("五六"), 6)

    def test_small_units(self):
        self.assertEqual(_cn_int("十五"), 15)
        self.assertEqual(_cn_int("一百五十六"), 156)
        self.assertEqual(_cn_int("三千"), 3000)

    def test_unit_without_leading_number_defaults_to_one(self):
        self.assertEqual(_cn_int("十"), 10)
        self.assertEqual(_cn_int("百"), 100)
        self.assertEqual(_cn_int("千"), 1000)

    def test_zero_marker_keeps_place_value(self):
        self.assertEqual(_cn_int("一百零五"), 105)

    def test_illegal_character_returns_zero(self):
        self.assertEqual(_cn_int("一x"), 0.0)

    def test_empty_returns_zero(self):
        self.assertEqual(_cn_int(""), 0.0)


class CnToNumberTest(unittest.TestCase):
    def test_empty_and_blank(self):
        self.assertEqual(cn_to_number(""), 0.0)
        self.assertEqual(cn_to_number(None), 0.0)

    def test_plain_numbers(self):
        self.assertEqual(cn_to_number("1500000"), 1500000.0)
        self.assertEqual(cn_to_number("1.15"), 1.15)
        self.assertEqual(cn_to_number(" 12 "), 12.0)

    def test_chinese_int(self):
        self.assertEqual(cn_to_number("一百五十六"), 156)
        self.assertEqual(cn_to_number("两"), 2)

    def test_wan_section(self):
        self.assertEqual(cn_to_number("一百五十万"), 1500000)
        self.assertEqual(cn_to_number("两万"), 20000)
        self.assertEqual(cn_to_number("十二万五千"), 125000)

    def test_yi_section(self):
        self.assertEqual(cn_to_number("亿"), 1e8)
        self.assertEqual(cn_to_number("一亿二千万"), 1.2e8)

    def test_colloquial_short_tail(self):
        self.assertEqual(cn_to_number("一万二"), 12000)
        self.assertEqual(cn_to_number("三万五"), 35000)

    def test_full_tail_is_not_shortened(self):
        self.assertEqual(cn_to_number("一万零二"), 10002)

    def test_illegal_text_returns_zero(self):
        self.assertEqual(cn_to_number("abc"), 0.0)


class ParseAmountTest(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertEqual(parse_amount(None), 0.0)
        self.assertEqual(parse_amount(""), 0.0)

    def test_no_amount_returns_zero(self):
        self.assertEqual(parse_amount("今天天气不错"), 0.0)

    def test_single_chinese_digit_is_noise(self):
        self.assertEqual(parse_amount("一年"), 0.0)

    def test_plain_number_is_accepted(self):
        self.assertEqual(parse_amount("3年"), 3.0)

    def test_wan_unit_wins(self):
        self.assertEqual(parse_amount("150万"), 1500000.0)
        self.assertEqual(parse_amount("1.15万"), 11500.0)
        self.assertEqual(parse_amount("两万"), 20000.0)

    def test_currency_adds_score_and_value_kept(self):
        self.assertEqual(parse_amount("20万元"), 200000.0)
        self.assertEqual(parse_amount("500块"), 500.0)

    def test_thousand_and_hundred_units_scaled(self):
        self.assertEqual(parse_amount("3千"), 3000.0)
        self.assertEqual(parse_amount("5百元"), 500.0)

    def test_thousands_separator(self):
        self.assertEqual(parse_amount("123,456元"), 123456.0)
        self.assertEqual(parse_amount("1,234万"), 12340000.0)

    def test_chinese_amount_inside_sentence(self):
        self.assertEqual(parse_amount("买车大概要一百五十万"), 1500000.0)

    def test_zero_value_is_skipped(self):
        self.assertEqual(parse_amount("零元"), 0.0)

    def test_best_candidate_is_chosen(self):
        self.assertEqual(parse_amount("一年打车费5000，买车要20万元"), 200000.0)

    def test_chinese_ten_is_not_negative_scored(self):
        self.assertEqual(parse_amount("十"), 10.0)


class FmtYuanTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(fmt_yuan(None), "未提供")

    def test_unconvertible(self):
        self.assertEqual(fmt_yuan("abc"), "abc")

    def test_exact_wan(self):
        self.assertEqual(fmt_yuan(1500000), "150万元")
        self.assertEqual(fmt_yuan("20000"), "2万元")

    def test_fraction_wan_keeps_two_decimals(self):
        self.assertEqual(fmt_yuan(12345), "1.23万元")

    def test_negative_value(self):
        self.assertEqual(fmt_yuan(-15000), "-1.5万元")

    def test_below_wan(self):
        self.assertEqual(fmt_yuan(3000), "3000元")
        self.assertEqual(fmt_yuan(0), "0元")

    def test_rounding_boundary(self):
        self.assertEqual(fmt_yuan(9999.6), "10000元")


class FmtIntTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(fmt_int(None), "未提供")

    def test_unconvertible(self):
        self.assertEqual(fmt_int("abc"), "abc")

    def test_thousands_separator(self):
        self.assertEqual(fmt_int(1500000), "1,500,000")
        self.assertEqual(fmt_int("12000"), "12,000")

    def test_rounding(self):
        self.assertEqual(fmt_int(3.7), "4")

    def test_object_is_stringified(self):
        obj = object()
        self.assertEqual(fmt_int(obj), str(obj))


if __name__ == "__main__":
    unittest.main()