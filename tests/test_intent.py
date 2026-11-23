"""入口接待的规则、模型兜底与默认放行。"""
import unittest

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.intent import Gate, judge, rule_gate, topic_of


class RuleGateTest(unittest.TestCase):
    def test_decision_words_pass(self):
        for text in ("我要不要买一台二十万级的家用车", "我该不该跳槽", "这两个方案选哪个",
                     "现在离职值得吗"):
            with self.subTest(text=text):
                gate = rule_gate(text)
                self.assertIsNotNone(gate)
                self.assertTrue(gate.is_decision)
                self.assertEqual(gate.source, "rule")

    def test_doi_topic_passes_without_question_form(self):
        gate = rule_gate("最近在纠结离职这件事")
        self.assertTrue(gate.is_decision)
        self.assertIn("离职", gate.topic)

    def test_doi_only_sentence_passes(self):
        gate = rule_gate("我想离职")
        self.assertTrue(gate.is_decision)
        self.assertIn("命中决策主题", gate.reason)
        self.assertEqual(gate.topic, "我想离职")

    def test_strong_non_decision_blocked(self):
        for text in ("帮我写个脚本", "这个报错了怎么修", "今天天气怎么样", "你是谁"):
            with self.subTest(text=text):
                gate = rule_gate(text)
                self.assertIsNotNone(gate)
                self.assertFalse(gate.is_decision)

    def test_short_greeting_blocked(self):
        gate = rule_gate("你好")
        self.assertFalse(gate.is_decision)
        self.assertIn("寒暄", gate.reason)

    def test_greeting_with_decision_passes(self):
        gate = rule_gate("你好，我在纠结要不要离职")
        self.assertTrue(gate.is_decision)

    def test_ambiguous_returns_none(self):
        self.assertIsNone(rule_gate("我最近在考虑一件事"))

    def test_empty_blocked(self):
        gate = rule_gate("   ")
        self.assertFalse(gate.is_decision)

    def test_topic_strips_question_prefix(self):
        self.assertEqual(topic_of("我要不要买一台二十万级的家用车"), "买一台二十万级的家用车")
        self.assertEqual(topic_of("该不该换城市？"), "换城市")
        self.assertEqual(topic_of(""), "")


class JudgeTest(unittest.TestCase):
    def test_without_probe_ambiguous_defaults_to_pass(self):
        gate = judge("我最近在考虑一件事")
        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.source, "fallback")

    def test_model_blocks_when_explicitly_not_decision(self):
        gate = judge("我最近在考虑一件事",
                     probe=lambda text: {"is_decision": False, "reason": "没有具体选择"})
        self.assertFalse(gate.is_decision)
        self.assertEqual(gate.source, "model")
        self.assertEqual(gate.reason, "没有具体选择")

    def test_model_missing_flag_defaults_to_pass(self):
        gate = judge("我最近在考虑一件事", probe=lambda text: {"topic": "考虑一件事"})
        self.assertTrue(gate.is_decision)

    def test_model_decision_uses_topic(self):
        gate = judge("我最近在考虑一件事",
                     probe=lambda text: {"is_decision": True, "topic": "换工作"})
        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.topic, "换工作")

    def test_probe_exception_defaults_to_pass(self):
        def boom(text):
            raise RuntimeError("模型挂了")

        gate = judge("我最近在考虑一件事", probe=boom)
        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.source, "fallback")

    def test_probe_bad_shape_defaults_to_pass(self):
        gate = judge("我最近在考虑一件事", probe=lambda text: "不是 JSON")
        self.assertTrue(gate.is_decision)


class GateDescribeTest(unittest.TestCase):
    def test_describe(self):
        self.assertIn("判定为决策", Gate(True, "命中", topic="买车").describe())
        self.assertIn("未进入沙盘", Gate(False, "寒暄").describe())


if __name__ == "__main__":
    unittest.main()