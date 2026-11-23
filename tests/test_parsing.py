import unittest

import _support  # noqa: F401

from core.jsonx import ParseError, parse_json_object
from core.numbers_cn import cn_to_number, fmt_yuan, parse_amount


class AmountTest(unittest.TestCase):
    def test_parse_amount(self):
        cases = {
            "一百五十万": 1500000,
            "1.15万": 11500,
            "一万二": 12000,
            "3000": 3000,
            "二十万": 200000,
            "每年两万": 20000,
            "150,000": 150000,
            "1,666 元": 1666,
            "没有数字": 0,
        }
        for text, expect in cases.items():
            self.assertAlmostEqual(parse_amount(text), expect, msg=text)

    def test_cn_number(self):
        self.assertEqual(cn_to_number("一万二"), 12000)
        self.assertEqual(cn_to_number("十二万五千"), 125000)
        self.assertEqual(cn_to_number("三千"), 3000)

    def test_format(self):
        self.assertEqual(fmt_yuan(1500000), "150万元")
        self.assertEqual(fmt_yuan(3000), "3000元")
        self.assertEqual(fmt_yuan(None), "未提供")


class JsonParseTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_object('{"a":1}'), {"a": 1})

    def test_code_fence(self):
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_noise_around(self):
        self.assertEqual(parse_json_object('好的，结果如下：\n{"a": {"b": 2}}\n以上。'), {"a": {"b": 2}})

    def test_braces_inside_string(self):
        self.assertEqual(parse_json_object('{"a": "}"}'), {"a": "}"})

    def test_array_rejected(self):
        with self.assertRaises(ParseError):
            parse_json_object("[1, 2, 3]")

    def test_empty(self):
        with self.assertRaises(ParseError):
            parse_json_object("")

    def test_oversized_text_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse_json_object("{" + "x" * 200000 + "}")
        self.assertIn("上限", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()