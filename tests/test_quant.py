"""core/quant.py 测试：量化口径必须唯一，真实模型不得自行选择公式。"""
import unittest

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core import quant
from core.mock import compute

REAL_ARCHIVE = {
    "finance": {"monthly_income": 16000, "cash": 800000,
                "monthly_saving": 4000, "max_one_time": 180000}
}
EMPTY_ARCHIVE = {"finance": {}}

BAD_MODEL_OUTPUT = {
    "side": "NEU",
    "round": 1,
    "cashflow": {"calc": "剩余现金 620000 ÷ 每月养车 1650 = 375.8", "one_time": 180000,
                 "monthly_delta": 1650, "months": 376, "boundary": "未越界",
                 "boundary_calc": "旧口径"},
    "thresholds": [
        {"name": "可动用存款下限", "value": "372000 元", "derive": "旧口径一"},
        {"name": "可动用存款下限", "value": "480000 元", "derive": "旧口径二"},
    ],
    "opportunity": {"calc": "180000 × 年化 3%～5%", "annual_low": 5400,
                    "annual_high": 9000, "range": "5400～9000 元/年"},
    "sensitivity": [],
}


class CoerceAmountTest(unittest.TestCase):
    def test_numeric_values_are_kept(self):
        self.assertEqual(quant.coerce_amount(16000), 16000)
        self.assertEqual(quant.coerce_amount(1.5), 1.5)

    def test_chinese_amounts_are_parsed(self):
        self.assertEqual(quant.coerce_amount("80万"), 800000)
        self.assertEqual(quant.coerce_amount("1.15万"), 11500)
        self.assertEqual(quant.coerce_amount("16,000 元"), 16000)

    def test_invalid_values_return_none(self):
        for value in (None, True, "", "看不清", -1, -2.5):
            with self.subTest(value=value):
                self.assertIsNone(quant.coerce_amount(value))


class MeasureTest(unittest.TestCase):
    def test_real_scenario_matches_mock_and_report(self):
        measured = quant.measure(REAL_ARCHIVE, 1650)
        self.assertEqual(measured["months"], 45.4)
        self.assertEqual(measured["cashflow"]["months"], 45.4)
        self.assertIn("800,000 - 180,000 = 620,000", measured["cashflow"]["calc"])
        self.assertIn("(12,000 + 1,650)", measured["cashflow"]["calc"])
        self.assertEqual(measured["cashflow"]["boundary"], "未越界")
        self.assertEqual(measured["thresholds"][0]["value"], "25.2万元")
        self.assertEqual(measured["thresholds"][1]["value"], "1,650 元/月")
        self.assertEqual(len(measured["thresholds"]), 2)

    def test_missing_finance_uses_assumptions(self):
        measured = quant.measure(EMPTY_ARCHIVE, None)
        self.assertEqual(measured["months"], 98.5)
        self.assertEqual(len(measured["notes"]), 5)
        self.assertIn("假设值", measured["cashflow"]["calc"])

    def test_archive_monthly_delta_overrides_default(self):
        archive = {"finance": {"monthly_income": 16000, "cash": 800000,
                               "monthly_saving": 4000, "max_one_time": 180000,
                               "monthly_delta": 1000}}
        measured = quant.measure(archive, None)
        self.assertEqual(measured["monthly_delta"], 1000)
        self.assertEqual(measured["months"], 47.7)
        self.assertNotIn("假设值", measured["cashflow"]["calc"])

    def test_monthly_need_is_floored_at_zero(self):
        archive = {"finance": {"monthly_income": 3000, "monthly_saving": 8000,
                               "cash": 100000, "max_one_time": 10000}}
        measured = quant.measure(archive, 1650)
        self.assertIn("(0 + 1,650)", measured["cashflow"]["calc"])
        self.assertEqual(measured["thresholds"][0]["value"], "1万元")

    def test_zero_burn_does_not_divide_by_zero(self):
        archive = {"finance": {"monthly_income": 3000, "monthly_saving": 3000,
                               "cash": 100000, "max_one_time": 10000}}
        measured = quant.measure(archive, 0)
        self.assertEqual(measured["months"], 0.0)

    def test_boundary_marks_overrun(self):
        archive = {"finance": {"monthly_income": 16000, "monthly_saving": 4000,
                               "cash": 200000, "max_one_time": 180000}}
        measured = quant.measure(archive, 1650)
        self.assertEqual(measured["cashflow"]["boundary"], "越界")

    def test_mock_compute_shares_the_same_arithmetic(self):
        mock_result = compute(REAL_ARCHIVE, 1650)
        measured = quant.measure(REAL_ARCHIVE, 1650)
        self.assertEqual(mock_result["cashflow"], measured["cashflow"])
        self.assertEqual(mock_result["thresholds"], measured["thresholds"])
        self.assertEqual(mock_result["sensitivity"], measured["sensitivity"])


class DeltaTest(unittest.TestCase):
    def test_cost_hint_with_annual_amount_wins(self):
        data = {"cashflow": {"monthly_delta": 1650}}
        self.assertEqual(quant.delta_from_neu(data, "养车成本每年 12000 元"), 1000)

    def test_cost_hint_without_year_keeps_monthly(self):
        data = {"cashflow": {"monthly_delta": 1650}}
        self.assertEqual(quant.delta_from_neu(data, "养车成本每月 2000 元"), 2000)

    def test_model_value_used_when_hint_is_silent(self):
        data = {"cashflow": {"monthly_delta": "1800 元/月"}}
        self.assertEqual(quant.delta_from_neu(data), 1800)

    def test_fallback_used_when_model_omits_delta(self):
        self.assertEqual(quant.delta_from_neu({}, fallback=1500), 1500)

    def test_previous_delta_beats_model_guess(self):
        # 非成本类重算时模型常自造 monthly_delta（实测 350000），必须沿用上一轮
        data = {"cashflow": {"monthly_delta": 350000}}
        self.assertEqual(quant.delta_from_neu(data, "收入差距被低估了", fallback=1650), 1650)

    def test_cost_hint_beats_previous_delta(self):
        data = {"cashflow": {"monthly_delta": 350000}}
        self.assertEqual(quant.delta_from_neu(data, "养车成本每年 12000 元", fallback=1650), 1000)

    def test_asserted_actual_amount_wins_over_quoted_amount(self):
        text = "反方说的养车成本每年两万太高了，我实际一年一万二"
        self.assertEqual(quant.cost_amount(text), 12000)
        self.assertEqual(quant.delta_from_neu({}, text), 1000)


class NormalizeTest(unittest.TestCase):
    def test_wrong_model_months_is_overridden(self):
        result = quant.normalize_neu(REAL_ARCHIVE, BAD_MODEL_OUTPUT)
        self.assertEqual(result["cashflow"]["months"], 45.4)
        self.assertNotIn("375.8", result["cashflow"]["calc"])
        self.assertIn("12,000 + 1,650", result["cashflow"]["calc"])
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["side"], "NEU")

    def test_conflicting_thresholds_collapse_to_one(self):
        result = quant.normalize_neu(REAL_ARCHIVE, BAD_MODEL_OUTPUT)
        self.assertEqual(len(result["thresholds"]), 2)
        self.assertEqual(result["thresholds"][0]["value"], "25.2万元")
        self.assertNotIn("372000", str(result["thresholds"]))
        self.assertNotIn("480000", str(result["thresholds"]))

    def test_sensitivity_is_recomputed_by_code(self):
        result = quant.normalize_neu(REAL_ARCHIVE, BAD_MODEL_OUTPUT)
        self.assertEqual(len(result["sensitivity"]), 2)
        self.assertIn("可支撑", result["sensitivity"][0]["up"])

    def test_opportunity_numbers_are_always_from_code(self):
        # 模型实测自造过「年化 2%～6%」和「按全部存款测算」，一律以代码 3%～5% 口径覆盖
        bogus = {"cashflow": {"months": 1},
                 "opportunity": {"calc": "1,500,000 × 2%～6%", "annual_low": 30000,
                                 "annual_high": 90000, "range": "3万～9万 元/年"}}
        result = quant.normalize_neu(REAL_ARCHIVE, bogus)
        self.assertEqual(result["opportunity"]["annual_low"], 5400)
        self.assertEqual(result["opportunity"]["annual_high"], 9000)
        self.assertIn("5,400～9,000 元/年", result["opportunity"]["range"])
        self.assertNotIn("30000", result["opportunity"]["calc"])

    def test_missing_opportunity_is_filled(self):
        result = quant.normalize_neu(REAL_ARCHIVE, {"cashflow": {"months": 1}})
        self.assertEqual(result["opportunity"]["annual_low"], 5400)
        self.assertEqual(result["opportunity"]["annual_high"], 9000)


class AlignTest(unittest.TestCase):
    def test_support_claim_is_corrected(self):
        text = "62万元覆盖必要月开支1.2万元，可支撑约51个月。"
        self.assertIn("可支撑约45.4 个月", quant.align_months_text(text, 45.4))

    def test_consistent_claim_is_untouched(self):
        text = "可支撑 45.4 个月，安全边界未越界"
        self.assertEqual(quant.align_months_text(text, 45.4), text)

    def test_emergency_fund_months_is_not_touched(self):
        text = "保留六个月必要开支作为应急金，即 6 个月"
        self.assertEqual(quant.align_months_text(text, 45.4), text)

    def test_report_is_aligned_recursively(self):
        report = {
            "summary": ["可支撑约51个月"],
            "plans": [{"name": "方案一", "cost": "可支撑 376 个月"}],
            "thresholds": 45.4,
        }
        aligned = quant.align_report(report, 45.4)
        self.assertIn("45.4 个月", aligned["summary"][0])
        self.assertNotIn("51", aligned["summary"][0])
        self.assertIn("45.4 个月", aligned["plans"][0]["cost"])
        self.assertEqual(aligned["thresholds"], 45.4)


class AuthorityTest(unittest.TestCase):
    def test_authority_text_matches_measure(self):
        authority = quant.authority(REAL_ARCHIVE, {"cashflow": {"monthly_delta": 1650}})
        self.assertEqual(authority["months"], 45.4)
        self.assertIn("45.4 个月", authority["thresholds_text"])
        self.assertIn("25.2万元", authority["thresholds_text"])
        self.assertIn("1,650 元/月", authority["prompt_text"])


class PeriodTest(unittest.TestCase):
    def test_future_year_monthly_rent_is_not_divided(self):
        self.assertEqual(quant.delta_from_neu({}, "明年的租金是5000"), 5000)
        self.assertEqual(quant.delta_from_neu({}, "明年开始每月租金5000"), 5000)

    def test_annual_cost_is_divided(self):
        self.assertEqual(quant.delta_from_neu({}, "每年的养车费用2万"), 1666.67)
        self.assertEqual(quant.delta_from_neu({}, "年养车成本20万"), 16666.67)


class NoBurnTest(unittest.TestCase):
    def test_zero_burn_is_flagged_and_not_shown_as_zero_months(self):
        archive = {"finance": {"monthly_income": 3000, "monthly_saving": 3000,
                               "cash": 100000, "max_one_time": 10000, "monthly_delta": 0}}
        measured = quant.measure(archive, None)
        self.assertTrue(measured["cashflow"]["no_burn"])
        self.assertIn("不适用", measured["cashflow"]["calc"])
        alert = quant.authority(archive, {"cashflow": {"monthly_delta": 0}})
        self.assertIsNone(alert["months"])
        self.assertIn("不适用", alert["thresholds_text"])

    def test_normal_burn_still_reports_months(self):
        alert = quant.authority(REAL_ARCHIVE, {"cashflow": {"monthly_delta": 1650}})
        self.assertEqual(alert["months"], 45.4)
        self.assertFalse(alert["no_burn"])


class InsufficientDataTest(unittest.TestCase):
    def test_two_missing_finance_fields_are_flagged(self):
        measured = quant.measure({"finance": {"monthly_income": 10000, "cash": 500000}}, None)
        self.assertTrue(measured["insufficient"])
        self.assertFalse(quant.measure(REAL_ARCHIVE, 1650)["insufficient"])

    def test_authority_warns_when_finance_is_insufficient(self):
        alert = quant.authority({"finance": {"monthly_income": 10000}}, None)
        self.assertIn("数据缺口", alert["prompt_text"])
        self.assertTrue(alert["insufficient"])


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        self.original = quant.configure()

    def tearDown(self):
        quant.configure(self.original)

    def test_custom_emergency_months_changes_threshold(self):
        quant.configure({"emergency_months": 9})
        measured = quant.measure(REAL_ARCHIVE, 1650)
        self.assertIn("9 个月应急金", measured["thresholds"][0]["derive"])

    def test_custom_sensitivity_and_opportunity(self):
        quant.configure({"sensitivity_pct": 0.1,
                         "opportunity_low": 0.04, "opportunity_high": 0.06})
        measured = quant.measure(REAL_ARCHIVE, 1650)
        self.assertIn("上浮 10%", measured["sensitivity"][0]["up"])
        self.assertIn("年化 4.0%～6.0%", measured["opportunity"]["calc"])

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            quant.configure({"emergency_months": 0})
        with self.assertRaises(ValueError):
            quant.configure({"sensitivity_pct": "abc"})
        with self.assertRaises(ValueError):
            quant.configure({"opportunity_low": 0.1, "opportunity_high": 0.05})

    def test_unknown_keys_are_ignored(self):
        conf = quant.configure({"unknown": 1})
        self.assertNotIn("unknown", conf)


class ExternalUseTest(unittest.TestCase):
    def test_only_verifiable_numbers_survive(self):
        archive = {"external": {"items": [{"id": 1, "facts": {
            "amounts": [{"value": 1650.0, "raw": "1650元"}],
            "rates": [{"value": 3.5, "raw": "3.5%"}]}}]}}
        data = {"external_use": {"cited": [
            {"id": 1, "value": 1650, "unit": "元/月", "key": "月供"},
            {"id": 1, "value": 9999, "unit": "元/月", "key": "编造"},
            {"id": 9, "value": 1650, "unit": "元/月", "key": "不存在"},
        ]}}
        cited = quant.validate_external_use(archive, data)
        self.assertEqual([entry["value"] for entry in cited], [1650])
        self.assertEqual(cited[0]["id"], 1)

    def test_item_without_facts_cannot_cite_numbers(self):
        archive = {"external": {"items": [{"id": 1, "title": "无正文"}]}}
        data = {"external_use": {"cited": [{"id": 1, "value": 100}]}}
        self.assertEqual(quant.validate_external_use(archive, data), [])


if __name__ == "__main__":
    unittest.main()