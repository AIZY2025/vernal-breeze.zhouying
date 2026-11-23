"""新增的两个 Agent 功能单元测试。

功能一：入口接待判定 —— LLMTeam.intent_probe / MockTeam.intent_probe 与 core.intent.judge 的衔接。
功能二：跨立场口径同步 —— LLMTeam.sync_reply / MockTeam.sync_reply 以及 Sandbox._sync 的摘要分支。

全部外部模型调用均用假 client 打桩，不发起任何真实网络请求。
"""
import json
import unittest
from unittest.mock import MagicMock, call, patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.agents import INTENT_SYSTEM, LLMTeam
from core.intent import judge, topic_of
from core.jsonx import ParseError
from core.mock import MockTeam
from core.orchestrator import Sandbox

DEFAULT_REPLY = "口径已对齐，无需调整"


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


class FakeClient:
    """记录每次 chat 调用；既能返回文本，也能抛异常。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages, temperature=0.4):
        self.calls.append({"system": system, "messages": messages, "temperature": temperature})
        if not self.replies:
            raise AssertionError("预置回复不足，调用次数超出预期")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_team(replies, temps=None):
    fixed = {"A1": "SYS-A1", "A2": "SYS-A2", "A3": "SYS-A3",
             "A4": "SYS-A4", "A5": "SYS-A5"}
    with patch("core.agents.load_prompt", side_effect=lambda key: fixed[key]):
        return LLMTeam(FakeClient(replies), temps)


class IntentProbeTest(unittest.TestCase):
    """LLMTeam.intent_probe：入口接待触发的单轮模型判定。"""

    def test_parses_object_with_dedicated_prompt_and_low_temperature(self):
        reply = dumps({"is_decision": True, "reason": "明确的选择", "topic": "买车"})
        team = make_team([reply], temps={"A1": 0.9, "PRO": 0.6})

        result = team.intent_probe("我要不要买台车")

        self.assertEqual(result, {"is_decision": True, "reason": "明确的选择", "topic": "买车"})
        self.assertEqual(len(team.client.calls), 1)
        chat = team.client.calls[0]
        self.assertEqual(chat["system"], INTENT_SYSTEM)
        self.assertIn("入口接待", chat["system"])
        self.assertIn("只输出 JSON", chat["system"])
        self.assertEqual(chat["messages"], [{"role": "user", "content": "我要不要买台车"}])
        self.assertEqual(chat["temperature"], 0.1)

    def test_fenced_json_is_cleaned_before_parsing(self):
        team = make_team(['```json\n{"is_decision": false, "reason": "寒暄"}\n```'])

        result = team.intent_probe("你好呀")

        self.assertIs(result["is_decision"], False)
        self.assertEqual(result["reason"], "寒暄")

    def test_invalid_json_raises_without_retry(self):
        team = make_team(["这不是 JSON"])

        with self.assertRaises(ParseError) as ctx:
            team.intent_probe("你好")

        self.assertIn("响应不是合法 JSON", str(ctx.exception))
        self.assertEqual(len(team.client.calls), 1)

    def test_json_array_without_object_is_rejected(self):
        team = make_team([dumps([True, False])])

        with self.assertRaises(ParseError) as ctx:
            team.intent_probe("要不要考研")

        self.assertIn("响应不是合法 JSON", str(ctx.exception))

    def test_object_inside_array_is_extracted(self):
        team = make_team([dumps([{"is_decision": False, "reason": "知识问答"}])])

        result = team.intent_probe("什么是复利")

        self.assertIs(result["is_decision"], False)
        self.assertEqual(result["reason"], "知识问答")

    def test_empty_reply_raises_empty_response_error(self):
        team = make_team([""])

        with self.assertRaises(ParseError) as ctx:
            team.intent_probe("要不要考研")

        self.assertEqual(str(ctx.exception), "空响应")
        self.assertEqual(len(team.client.calls), 1)

    def test_client_exception_propagates_after_single_call(self):
        team = make_team([RuntimeError("模型服务不可用")])

        with self.assertRaises(RuntimeError) as ctx:
            team.intent_probe("要不要考研")

        self.assertIn("模型服务不可用", str(ctx.exception))
        self.assertEqual(len(team.client.calls), 1)


class IntentGateIntegrationTest(unittest.TestCase):
    """入口接待：team.intent_probe 作为 probe 注入 core.intent.judge 后的分支。"""

    def test_model_blocks_with_truncated_reason_and_topic_fallback(self):
        text = "我最近一直在考虑一件事"
        team = make_team([dumps({"is_decision": False, "reason": "长" * 80, "topic": "   "})])

        gate = judge(text, team.intent_probe)

        self.assertFalse(gate.is_decision)
        self.assertEqual(gate.source, "model")
        self.assertEqual(len(gate.reason), 60)
        self.assertEqual(gate.reason, "长" * 60)
        self.assertEqual(gate.topic, topic_of(text))
        self.assertEqual(len(team.client.calls), 1)

    def test_model_decision_uses_and_truncates_model_topic(self):
        long_topic = "换" * 50
        team = make_team([dumps({"is_decision": True, "topic": long_topic})])

        gate = judge("我最近在考虑一件事", team.intent_probe)

        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.source, "model")
        self.assertEqual(len(gate.topic), 40)
        self.assertEqual(gate.topic, long_topic[:40])

    def test_probe_failure_defaults_to_pass_with_fallback_reason(self):
        team = make_team([RuntimeError("网络错误")])

        gate = judge("我最近在考虑一件事", team.intent_probe)

        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.source, "fallback")
        self.assertIn("入口判定失败", gate.reason)
        self.assertEqual(gate.topic, topic_of("我最近在考虑一件事"))

    def test_rule_hit_preempts_model_probe(self):
        team = make_team([])

        gate = judge("我要不要买一台二十万级的家用车", team.intent_probe)

        self.assertTrue(gate.is_decision)
        self.assertEqual(gate.source, "rule")
        self.assertEqual(gate.topic, "买一台二十万级的家用车")
        self.assertEqual(team.client.calls, [])

    def test_mock_team_probe_returns_empty_and_leaves_judgement_to_rules(self):
        team = MockTeam()

        self.assertEqual(team.intent_probe("任何输入"), {})

        blocked = judge("你好", team.intent_probe)
        self.assertFalse(blocked.is_decision)
        self.assertEqual(blocked.source, "rule")
        self.assertIn("寒暄", blocked.reason)

        passed = judge("我最近在考虑一件事", team.intent_probe)
        self.assertTrue(passed.is_decision)
        self.assertEqual(passed.source, "fallback")
        self.assertEqual(passed.reason, "入口判定未返回结论，按放行处理")
        self.assertEqual(passed.topic, topic_of("我最近在考虑一件事"))


class LLMSyncReplyTest(unittest.TestCase):
    """LLMTeam.sync_reply：对手方一句回复的兜底与截断边界。"""

    def test_whitespace_only_reply_falls_back_to_default(self):
        team = make_team([dumps({"reply": "   "})])

        self.assertEqual(team.sync_reply("PRO", {"arguments": []}, "摘要"), DEFAULT_REPLY)

    def test_unparsable_multiline_reply_keeps_first_line_only(self):
        team = make_team(["\n 据此同步调整第4条\n第二行补充说明"])

        self.assertEqual(team.sync_reply("CON", {"risks": []}, "摘要"), "据此同步调整第4条")

    def test_json_reply_survives_trailing_explanation(self):
        team = make_team(['{"reply":"据此同步调整第3条"} 其余说明'])

        self.assertEqual(team.sync_reply("CON", {}, "反方下调成本口径"), "据此同步调整第3条")

    def test_whitespace_is_stripped_before_truncation(self):
        team = make_team([dumps({"reply": "   口径已对齐，无需调整   "})])

        self.assertEqual(team.sync_reply("PRO", {}, "摘要"), "口径已对齐，无需调整")


class MockSyncReplyTest(unittest.TestCase):
    """MockTeam.sync_reply：预置剧本下的确定性回应。"""

    def test_returns_fixed_sentence_for_both_sides(self):
        team = MockTeam()
        pro_own = {"arguments": [{"claim": "效率更高"}]}
        con_own = {"risks": [{"desc": "现金流压力"}]}

        self.assertEqual(team.sync_reply("PRO", pro_own, "反方下调成本口径"), "口径已对齐，无需调整。")
        self.assertEqual(team.sync_reply("CON", con_own, "正方补充依据"), "口径已对齐，无需调整。")

    def test_does_not_mutate_own_payload(self):
        team = MockTeam()
        own = {"arguments": [{"claim": "效率更高", "basis": "通勤时间下降"}]}
        before = json.loads(json.dumps(own))

        team.sync_reply("PRO", own, "摘要")

        self.assertEqual(own, before)


class SandboxSyncTest(unittest.TestCase):
    """Sandbox._sync：摘要长度分支与跨方抄送方向。"""

    def _box(self, reply="口径已对齐，无需调整。", version=2):
        team = MagicMock()
        team.sync_reply.return_value = reply
        box = Sandbox.__new__(Sandbox)
        box.team = team
        box.ui = MagicMock()
        box.store = MagicMock()
        box.version = version
        box.pro = {"arguments": [{"claim": "正方原论据"}]}
        box.con = {"risks": [{"desc": "反方原风险", "level": "中"}]}
        box.syncs = []
        box.drift = []
        return box, team

    def test_short_revision_builds_arrow_summary_and_targets_opponent(self):
        box, team = self._box()
        revision = {"item": "隐性持有成本", "from": "中", "to": "低",
                    "basis": "用户实测年成本"}

        box._sync("A2", revision)

        expected = "正方·隐性持有成本：中→低（依据：用户实测年成本）"
        self.assertEqual(team.sync_reply.call_args, call("CON", box.con, expected))
        self.assertEqual(len(box.syncs), 1)
        record = box.syncs[0]
        self.assertEqual(record["version"], 3)
        self.assertEqual(record["from"], "正方")
        self.assertEqual(record["to"], "反方")
        self.assertEqual(record["summary"], expected)
        self.assertEqual(record["length"], len(expected))
        self.assertEqual(record["reply"], "口径已对齐，无需调整。")
        box.store.log.assert_called_once_with(
            "A5 口径同步", "正方→反方：%s" % expected, level="line")
        box.ui.info.assert_called_once_with(
            "口径同步（%d字）→ A3 ｜ 回应：%s" % (len(expected), "口径已对齐，无需调整。"))

    def test_long_revision_uses_rewritten_summary(self):
        box, team = self._box()
        revision = {"item": "现金流压力", "from": "中" * 20, "to": "低" * 20,
                    "basis": "用户提供实测数据"}

        box._sync("A3", revision)

        expected = "反方·现金流压力已修订（依据：用户提供实测数据）"
        self.assertEqual(team.sync_reply.call_args, call("PRO", box.pro, expected))
        self.assertEqual(box.syncs[0]["from"], "反方")
        self.assertEqual(box.syncs[0]["to"], "正方")
        self.assertEqual(box.syncs[0]["summary"], expected)

    def test_summary_is_truncated_to_fifty_chars(self):
        box, team = self._box()
        revision = {"item": "成本口径", "from": "甲" * 40, "to": "乙" * 40,
                    "basis": "依据" * 20}

        box._sync("A2", revision)

        summary = team.sync_reply.call_args[0][2]
        self.assertEqual(len(summary), 50)
        self.assertTrue(summary.startswith("正方·成本口径已修订（依据："))
        self.assertEqual(box.syncs[0]["length"], 50)

    def test_missing_fields_use_defaults(self):
        box, team = self._box()

        box._sync("A2", {})

        expected = "正方·论据：→（依据：）"
        self.assertEqual(team.sync_reply.call_args, call("CON", box.con, expected))
        self.assertEqual(box.syncs[0]["summary"], expected)

    def test_opponent_payload_is_copied_before_sync(self):
        box, team = self._box()
        original = box.con

        box._sync("A2", {"item": "论据", "from": "旧", "to": "新", "basis": "依据"})

        passed = team.sync_reply.call_args[0][1]
        self.assertEqual(passed, original)
        self.assertIsNot(passed, original)


if __name__ == "__main__":
    unittest.main()