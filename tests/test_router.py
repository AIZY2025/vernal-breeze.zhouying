import unittest

import _support  # noqa: F401  （负责把项目根目录加入 sys.path）

from core.router import _extract_params, classify, extract_params


class ClassifyTest(unittest.TestCase):
    def test_close(self):
        route = classify("结束沙盘，给我结论")
        self.assertEqual(route.intent, "CLOSE")
        self.assertEqual(route.target, "A5")

    def test_challenge_con(self):
        route = classify("反方说的养车成本每年两万太高了，我实际一年一万二")
        self.assertEqual(route.intent, "CHALLENGE_CON")
        self.assertEqual(route.target, "A3")

    def test_challenge_pro(self):
        route = classify("我质疑正方第一条论据，效率提升没那么大")
        self.assertEqual(route.intent, "CHALLENGE_PRO")
        self.assertEqual(route.target, "A2")

    def test_param(self):
        route = classify("我把可动用存款改成150万")
        self.assertEqual(route.intent, "PARAM")
        self.assertEqual(route.params.get("cash"), 1500000)

    def test_new_fact(self):
        self.assertEqual(classify("补充一个情况：公司明年搬到南山").intent, "NEW_FACT")

    def test_unknown(self):
        self.assertEqual(classify("今天天气不错").intent, "UNKNOWN")

    def test_stop(self):
        self.assertEqual(classify("停止推演").intent, "STOP")
        self.assertEqual(classify("不继续了").intent, "STOP")
        self.assertEqual(classify("停止").intent, "STOP")

    def test_stop_beats_close(self):
        self.assertEqual(classify("停止推演，不想要结论了").intent, "STOP")

    def test_stop_word_inside_sentence_is_not_a_command(self):
        self.assertNotEqual(classify("我决定停止吸烟后的开支").intent, "STOP")

    def test_colloquial_close(self):
        self.assertEqual(classify("不买了，就到这").intent, "CLOSE")
        self.assertEqual(classify("不用了，先这样").intent, "CLOSE")

    def test_colloquial_challenge(self):
        self.assertEqual(classify("这个风险不现实，站不住脚").intent, "CHALLENGE_CON")

    def test_colloquial_new_fact(self):
        self.assertEqual(classify("我刚想到一个事：公司可能裁员").intent, "NEW_FACT")

    def test_number_without_field(self):
        route = classify("150万")
        self.assertEqual(route.intent, "PARAM")
        self.assertEqual(route.params, {})


class ParamExtractTest(unittest.TestCase):
    def test_public_extract_params_alias(self):
        self.assertIs(extract_params, _extract_params)
        self.assertEqual(extract_params("我养车每年1.5万"), {"monthly_delta": 1250})

    def test_neighbour_amount_not_leaked_into_cash(self):
        self.assertEqual(_extract_params("月收入1.5万，存款不变"), {"monthly_income": 15000})
        self.assertEqual(_extract_params("存款不变，月收入1.5万"), {"monthly_income": 15000})

    def test_annual_car_cost_converted_to_monthly(self):
        self.assertEqual(_extract_params("我养车每年1.5万"), {"monthly_delta": 1250})

    def test_monthly_car_cost_kept(self):
        self.assertEqual(_extract_params("我每月养车1500元"), {"monthly_delta": 1500})

    def test_amount_before_key(self):
        self.assertEqual(_extract_params("150万存款"), {"cash": 1500000})

    def test_negation_does_not_change_value(self):
        self.assertEqual(_extract_params("存款保持不变"), {})
        self.assertEqual(_extract_params("存款还是原来那些"), {})

    def test_multiple_fields(self):
        params = _extract_params("月收入改成1.5万，可动用存款150万")
        self.assertEqual(params.get("monthly_income"), 15000)
        self.assertEqual(params.get("cash"), 1500000)

    def test_thousands_separator(self):
        self.assertEqual(_extract_params("存款150,000"), {"cash": 150000})
        self.assertEqual(_extract_params("可动用存款 1,200,000 元"), {"cash": 1200000})

    def test_annual_cost_with_thousands_separator(self):
        self.assertEqual(_extract_params("养车每年 12,000 元"), {"monthly_delta": 1000})

    def test_hand_cash(self):
        self.assertEqual(_extract_params("手头有150万"), {"cash": 1500000})

    def test_explicit_negative_amount_kept_for_rejection(self):
        self.assertEqual(_extract_params("把月收入改成-100"), {"monthly_income": -100})
        self.assertEqual(_extract_params("月收入改成负一百"), {"monthly_income": -100})

    def test_huge_amount_still_extracted_for_limit_check(self):
        self.assertEqual(_extract_params("把月收入改成999999999"),
                         {"monthly_income": 999999999})


class CommandBoundaryTest(unittest.TestCase):
    def test_negated_stop_is_not_stop(self):
        self.assertNotEqual(classify("不要继续用打车方案了，我想改成买车").intent, "STOP")
        self.assertNotEqual(classify("我不继续等了，决定买").intent, "STOP")

    def test_negated_or_conditional_close_is_not_close(self):
        self.assertEqual(classify("还没结束，补充一个情况：公司搬到南山").intent, "NEW_FACT")
        self.assertNotEqual(classify("别结束，我再想想").intent, "CLOSE")
        route = classify("结束之前先把存款改成150万")
        self.assertEqual(route.intent, "PARAM")
        self.assertEqual(route.params.get("cash"), 1500000)

    def test_param_beats_challenge_when_value_is_changed(self):
        route = classify("我把存款改到150万，但风险还是太高")
        self.assertEqual(route.intent, "PARAM")
        self.assertEqual(route.params.get("cash"), 1500000)

    def test_cost_challenge_still_routes_to_con(self):
        route = classify("反方说的养车成本每年两万太高了，我实际一年一万二")
        self.assertEqual(route.intent, "CHALLENGE_CON")


class PeriodAndPercentTest(unittest.TestCase):
    def test_year_before_keyword_is_converted(self):
        self.assertEqual(_extract_params("年养车成本改成20万"), {"monthly_delta": 16666.67})
        self.assertEqual(_extract_params("年养车费用2万"), {"monthly_delta": 1666.67})
        self.assertEqual(_extract_params("每年的养车费用2万"), {"monthly_delta": 1666.67})

    def test_month_wins_over_future_year(self):
        self.assertEqual(_extract_params("明年开始每月租金5000"), {"monthly_delta": 5000})

    def test_percent_is_not_treated_as_amount(self):
        self.assertEqual(_extract_params("首付30%，总价200万"), {"max_one_time": 2000000})
        self.assertEqual(_extract_params("预算20万，首付5万"), {"max_one_time": 200000})
        self.assertEqual(classify("首付30%").intent, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()