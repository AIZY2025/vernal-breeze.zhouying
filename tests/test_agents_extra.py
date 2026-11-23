"""core/agents.py 分支补充测试：client.chat 全部打桩，校验提示词、温度、契约重试与截断逻辑。"""
import json
import unittest
from unittest.mock import patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.agents import LLMTeam, _cross_brief, _external_prompt
from core.contract import ContractError
from core.jsonx import ParseError

PRO_OK = {"arguments": [{"claim": "效率更高", "basis": "通勤时间下降"}]}
CON_OK = {"risks": [{"desc": "现金流压力", "level": "中"}]}
NEU_OK = {"cashflow": {"months": 30.0, "monthly_delta": 1650},
          "thresholds": [{"name": "下限"}]}
BOARD_OK = {"brief": ["a", "b"], "dispute": ["x"], "conclusion": {
    "verdict": "执行主方案", "reason": "安全边界未越界，可支撑 45.4 个月",
    "conditions": ["存款跌破阈值"]}}
REPORT_OK = {"report": {
    "verdict": "执行主方案", "reason": "安全边界未越界，可支撑 45.4 个月",
    "summary": ["s"], "compare": ["c"], "plans": ["p"],
    "switch": [{"indicator": "存款跌破阈值", "action": "改选方案二"}],
    "fallback": ["f"]}}
ARCHIVE_OK = {
    "question": "x", "type": "消费支出", "window": "三个月内",
    "options": [{"id": "A", "name": "执行主方案", "is_main": True}],
    "finance": {"monthly_income": None, "cash": None,
                "monthly_saving": None, "max_one_time": None},
    "needs": ["1. 降低通勤成本"],
    "risk_tolerance": "有限",
    "facts": [],
    "unknowns": [],
    "completeness": "基本充足",
    "gap": "财务四项未提供"}
CROSS_OK = {"side": "PRO", "cross": [
    {"target": "反方风险一", "verdict": "接受", "point": "该风险成立", "basis": "档案事实"}]}


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages, temperature=0.4):
        self.calls.append({"system": system, "messages": messages, "temperature": temperature})
        if not self.replies:
            raise AssertionError("预置回复不足")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_team(replies, temps=None, prompts=None):
    fixed = prompts or {"A1": "SYS-A1", "A2": "SYS-A2", "A3": "SYS-A3",
                        "A4": "SYS-A4", "A5": "SYS-A5"}
    with patch("core.agents.load_prompt", side_effect=lambda key: fixed[key]):
        team = LLMTeam(FakeClient(replies), temps)
    return team


class InitTest(unittest.TestCase):
    def test_prompts_loaded_for_five_agents(self):
        team = make_team([])
        self.assertEqual(team.prompts["A1"], "SYS-A1")
        self.assertEqual(team.prompts["A5"], "SYS-A5")
        self.assertEqual(team.temps, {})

    def test_temperature_dict_is_kept(self):
        team = make_team([], temps={"A1": 0.9})
        self.assertEqual(team.temps, {"A1": 0.9})


class RunRetryTest(unittest.TestCase):
    def test_parse_error_then_success_sends_correction_prompt(self):
        team = make_team(["不是 JSON", dumps({"questions": ["q1"]})])
        result = team.a1_questions("要不要买车", "答：预算 20 万", 2)
        self.assertEqual(result, {"questions": ["q1"]})
        self.assertEqual(len(team.client.calls), 2)
        second = team.client.calls[1]["messages"][0]["content"]
        self.assertIn("【修正要求】上一次输出无法解析为 JSON", second)
        self.assertIn("请严格按输出契约只输出一个 JSON 对象", second)
        self.assertTrue(second.startswith("用户原始问题：要不要买车"))

    def test_contract_error_hint_uses_exception_message(self):
        team = make_team([dumps({"questions": []}), dumps({"questions": ["q"]})])
        result = team.a1_questions("问题", "", 0)
        self.assertEqual(result, {"questions": ["q"]})
        second = team.client.calls[1]["messages"][0]["content"]
        self.assertIn("A1 输出缺少有效字段 questions", second)

    def test_gives_up_after_two_attempts(self):
        team = make_team(["坏数据一", "坏数据二"])
        with self.assertRaises(ParseError) as ctx:
            team.a1_questions("问题", "", 0)
        self.assertIn("坏数据二", str(ctx.exception))
        self.assertEqual(len(team.client.calls), 2)

    def test_temperature_from_temps_and_float_cast(self):
        team = make_team([dumps({"questions": ["q"]})], temps={"A1": "0.75"})
        team.a1_questions("问题", "", 0)
        self.assertEqual(team.client.calls[0]["temperature"], 0.75)
        self.assertEqual(team.client.calls[0]["system"], "SYS-A1")

    def test_happy_path_has_no_correction_hint(self):
        team = make_team([dumps({"questions": ["q"]})])
        team.a1_questions("问题", "", 1)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertNotIn("【修正要求】", user)
        self.assertEqual(team.client.calls[0]["messages"][0]["role"], "user")


class A1PromptTest(unittest.TestCase):
    def test_questions_formatting_and_remaining_count(self):
        team = make_team([dumps({"questions": ["q"]})])
        team.a1_questions("买车吗", "Q: 预算\nA: 20万", 2)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("用户原始问题：买车吗", user)
        self.assertIn("Q: 预算\nA: 20万", user)
        self.assertIn("已累计提问：2 个", user)
        self.assertIn("仅作参考", user)
        self.assertNotIn("系统安全阀", user)
        self.assertIn("逐项追问到底", user)
        self.assertNotIn("硬上限 9 个", user)

    def test_questions_empty_history_placeholder(self):
        team = make_team([dumps({"questions": ["q"]})])
        team.a1_questions("买车吗", "", 7)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("（尚未提问）", user)
        self.assertIn("已累计提问：7 个", user)
        self.assertIn("仅作参考", user)
        self.assertNotIn("系统安全阀", user)

    def test_archive_without_previous(self):
        team = make_team([dumps({"archive": ARCHIVE_OK})])
        result = team.a1_archive("买车吗", "", 3)
        self.assertEqual(result["archive"]["question"], "x")
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("（单一轮次）", user)
        self.assertIn("累计提问 3 个", user)
        self.assertIn("《决策背景档案》", user)
        self.assertEqual(team.client.calls[0]["system"], "SYS-A1")

    def test_archive_with_previous_archive(self):
        prev = dict(ARCHIVE_OK, question="买车吗", facts=["预算 20 万"])
        team = make_team([dumps({"archive": prev})])
        result = team.a1_archive("买车吗", "", 3, new_facts="停车位 500 元/月", prev_archive=prev)
        self.assertEqual(result["archive"]["facts"], ["预算 20 万"])
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("已有《决策背景档案》", user)
        self.assertIn("用户补充的新事实：停车位 500 元/月", user)
        self.assertIn('"预算 20 万"', user)

    def test_archive_empty_previous_archive_uses_first_branch(self):
        team = make_team([dumps({"archive": ARCHIVE_OK})])
        team.a1_archive("买车吗", "", 1, prev_archive={})
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("（单一轮次）", user)
        self.assertNotIn("已有《决策背景档案》", user)


class JudgePromptTest(unittest.TestCase):
    def test_judge_pro(self):
        team = make_team([dumps(PRO_OK)])
        self.assertEqual(team.judge_pro({"question": "x"}, 2), PRO_OK)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A2")
        self.assertIn("本轮为第 2 轮研判，请输出支持方 JSON", call["messages"][0]["content"])

    def test_judge_con(self):
        team = make_team([dumps(CON_OK)])
        self.assertEqual(team.judge_con({"question": "x"}), CON_OK)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A3")
        self.assertIn("本轮为第 1 轮研判，请输出反对方 JSON", call["messages"][0]["content"])

    def test_judge_neu_without_hint(self):
        team = make_team([dumps(NEU_OK)])
        result = team.judge_neu({"question": "x"}, 3)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertEqual(team.client.calls[0]["system"], "SYS-A4")
        self.assertNotIn("补充说明", user)
        self.assertIn("本轮为第 3 轮测算，请输出量化 JSON", user)
        self.assertEqual(result["cashflow"]["months"], 98.5)
        self.assertEqual(len(result["thresholds"]), 2)

    def test_judge_neu_with_hint(self):
        team = make_team([dumps(NEU_OK)])
        team.judge_neu({"question": "x"}, 1, hint="养车每年 2 万")
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("补充说明：养车每年 2 万", user)


class RevisePromptTest(unittest.TestCase):
    def test_revise_pro(self):
        prev = {"arguments": [{"claim": "旧论据", "basis": "旧依据"}]}
        team = make_team([dumps(PRO_OK)])
        self.assertEqual(team.revise_pro({"question": "x"}, prev, "效率没那么高", 4), PRO_OK)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A2")
        user = call["messages"][0]["content"]
        self.assertIn("你上一轮输出：", user)
        self.assertIn("用户质疑原话：效率没那么高", user)
        self.assertIn("第 4 轮", user)

    def test_revise_con(self):
        prev = {"risks": [{"desc": "旧风险", "level": "中"}]}
        team = make_team([dumps(CON_OK)])
        self.assertEqual(team.revise_con({"question": "x"}, prev, "成本没那么高", 2), CON_OK)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertEqual(team.client.calls[0]["system"], "SYS-A3")
        self.assertIn("回应用户质疑中的事实部分", user)
        self.assertIn("第 2 轮", user)

    def test_recompute_neu_without_structured_change(self):
        team = make_team([dumps(NEU_OK)])
        result = team.recompute_neu({"question": "x"}, NEU_OK, "把成本改成每年一万五", [], 5)
        self.assertEqual(result["cashflow"]["months"], 102.6)
        self.assertEqual(result["cashflow"]["monthly_delta"], 1250)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("无结构化变更，请从用户指令中提取", user)
        self.assertIn("第 5 轮", user)

    def test_recompute_neu_with_structured_change(self):
        team = make_team([dumps(NEU_OK)])
        changed = [{"name": "max_one_time", "value": 180000}]
        team.recompute_neu({"question": "x"}, NEU_OK, "改成 18 万", changed, 6)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn('"name": "max_one_time"', user)
        self.assertIn('"value": 180000', user)
        self.assertNotIn("无结构化变更", user)


class CrossExaminePromptTest(unittest.TestCase):
    def test_cross_pro_reads_opponent_and_forbids_new_arguments(self):
        team = make_team([dumps(CROSS_OK)])
        result = team.cross_pro({"question": "x"}, PRO_OK, CON_OK, 2)
        self.assertEqual(result, CROSS_OK)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A2")
        user = call["messages"][0]["content"]
        self.assertIn("你本轮已提交的正方论据", user)
        self.assertIn("反方本轮提出的三条风险", user)
        self.assertIn("不得新增己方论据", user)
        self.assertIn("第 2 轮", user)

    def test_cross_con_reads_opponent_and_forbids_new_risks(self):
        reply = {"side": "CON", "cross": [
            {"target": "正方论据一", "verdict": "限定", "point": "仅在通勤频繁时成立",
             "basis": "档案未确认用车频率"}]}
        team = make_team([dumps(reply)])
        result = team.cross_con({"question": "x"}, CON_OK, PRO_OK, 1)
        self.assertEqual(result, reply)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A3")
        user = call["messages"][0]["content"]
        self.assertIn("你本轮已提交的反方风险", user)
        self.assertIn("正方本轮提出的三条论据", user)
        self.assertIn("不得新增己方风险", user)


class QuantOverrideTest(unittest.TestCase):
    """BUG-SBX-002：真实模型把 45.4 个月算成 376 个月，代码必须覆盖。"""

    ARCHIVE = {"finance": {"monthly_income": 16000, "cash": 800000,
                           "monthly_saving": 4000, "max_one_time": 180000}}
    BAD_NEU = {
        "cashflow": {"calc": "620000÷1650=375.8", "months": 376, "monthly_delta": 1650,
                     "boundary": "未越界"},
        "thresholds": [{"name": "可动用存款下限", "value": "372000 元"}],
    }

    def test_judge_neu_overrides_wrong_months(self):
        team = make_team([dumps(self.BAD_NEU)])
        result = team.judge_neu(self.ARCHIVE, 1)
        self.assertEqual(result["cashflow"]["months"], 45.4)
        self.assertNotIn("375.8", result["cashflow"]["calc"])
        self.assertIn("12,000 + 1,650", result["cashflow"]["calc"])
        self.assertEqual(result["thresholds"][0]["value"], "25.2万元")

    def test_final_report_injects_authority_caliber(self):
        team = make_team([dumps(REPORT_OK)])
        data = team.final_report(self.ARCHIVE, PRO_OK, CON_OK, NEU_OK, 2, "")
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("【权威量化口径", user)
        self.assertIn("45.4 个月", user)
        self.assertIn("25.2万元", user)
        self.assertIn("45.4 个月", data["report"]["thresholds"])
        self.assertNotIn("30.0", data["report"]["thresholds"])


class BoardPromptTest(unittest.TestCase):
    def test_arbitrate_trims_drift_and_syncs(self):
        drift = [{"d": i} for i in range(6)]
        syncs = [{"s": i} for i in range(3)]
        team = make_team([dumps(BOARD_OK)])
        result = team.arbitrate({"question": "x"}, PRO_OK, CON_OK, NEU_OK, 3, drift, syncs, "用户要求重算")
        self.assertEqual(result, BOARD_OK)
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A5")
        user = call["messages"][0]["content"]
        self.assertIn("当前版本：V3", user)
        self.assertIn("本轮路由：用户要求重算", user)
        self.assertIn('"d": 2', user)
        self.assertIn('"d": 5', user)
        self.assertNotIn('"d": 1', user)
        self.assertIn('"s": 1', user)
        self.assertIn('"s": 2', user)
        self.assertNotIn('"s": 0', user)

    def test_final_report_with_rounds_text(self):
        team = make_team([dumps(REPORT_OK)])
        data = team.final_report({"question": "x"}, PRO_OK, CON_OK, NEU_OK, 4, "第1轮摘要")
        self.assertEqual(data["report"]["summary"], ["s"])
        self.assertIn("98.5 个月", data["report"]["thresholds"])
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("当前看板版本：V4", user)
        self.assertIn("第1轮摘要", user)
        self.assertIn("session_closed 为 true", user)

    def test_final_report_empty_rounds_text(self):
        team = make_team([dumps(REPORT_OK)])
        team.final_report({"question": "x"}, PRO_OK, CON_OK, NEU_OK, 1, "")
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("（无额外轮次）", user)


class SyncReplyTest(unittest.TestCase):
    def test_pro_side_prompt(self):
        team = make_team([dumps({"reply": "  口径已对齐  "})], temps={"PRO": "0.6"})
        reply = team.sync_reply("PRO", {"arguments": []}, "反方下调成本口径")
        self.assertEqual(reply, "口径已对齐")
        call = team.client.calls[0]
        self.assertIn("你是「春风」里的支持方", call["system"])
        self.assertNotIn("你是「春风」里的反对方", call["system"])
        self.assertEqual(call["temperature"], 0.6)
        self.assertIn("对手方修订摘要（不超过五十字）：反方下调成本口径", call["messages"][0]["content"])

    def test_con_side_prompt_and_default_temperature(self):
        team = make_team([dumps({"reply": "据此同步调整第2条"})])
        reply = team.sync_reply("CON", {"risks": []}, "正方补充依据")
        self.assertEqual(reply, "据此同步调整第2条")
        call = team.client.calls[0]
        self.assertIn("你是「春风」里的反对方", call["system"])
        self.assertEqual(call["temperature"], 0.5)

    def test_reply_is_truncated_to_thirty_chars(self):
        long_reply = "据此同步调整第1条并重写全部论据说明" * 3
        team = make_team([dumps({"reply": long_reply})])
        reply = team.sync_reply("PRO", {}, "摘要")
        self.assertEqual(len(reply), 30)
        self.assertEqual(reply, long_reply[:30])

    def test_invalid_json_falls_back_to_first_line(self):
        team = make_team(["第一行不能解析\n第二行也不能"])
        reply = team.sync_reply("PRO", {}, "摘要")
        self.assertEqual(reply, "第一行不能解析")

    def test_empty_raw_returns_default(self):
        team = make_team([""])
        self.assertEqual(team.sync_reply("PRO", {}, "摘要"), "口径已对齐，无需调整")

    def test_json_without_reply_returns_default(self):
        team = make_team([dumps({"other": 1})])
        self.assertEqual(team.sync_reply("CON", {}, "摘要"), "口径已对齐，无需调整")

    def test_non_string_reply_is_stringified(self):
        team = make_team([dumps({"reply": 123})])
        self.assertEqual(team.sync_reply("PRO", {}, "摘要"), "123")


class StatsTest(unittest.TestCase):
    def test_call_count_is_tracked(self):
        team = make_team([dumps(PRO_OK), dumps(PRO_OK)])
        team.judge_pro({"question": "x"}, 1)
        team.judge_pro({"question": "x"}, 2)
        stats = team.stats()
        self.assertEqual(stats["calls"], 2)
        self.assertGreaterEqual(stats["seconds"], 0.0)


class ExternalPromptTest(unittest.TestCase):
    """A4 提示词里的外部情报清单：只暴露真实编号与可量化数字。"""

    def test_missing_items_declares_no_quote(self):
        text = _external_prompt({"external": {"items": []}})
        self.assertIn("【外部情报】未获取", text)
        self.assertIn("cited 必须为空", text)
        self.assertIn("不得声称存在外部数据", text)

    def test_items_are_rendered_with_verified_values(self):
        archive = {"external": {"items": [
            {"id": 1, "title": "购车补贴实施细则（2026 年最新版）",
             "facts": {"amounts": [
                 {"raw": "1.2万元", "value": 12000.0},
                 {"raw": "", "value": 1},
                 {"raw": "3000元", "value": 3000.0},
                 {"raw": "超限额", "value": 9.0}],
                 "rates": [{"raw": "3.5%", "value": 3.5}],
                 "keywords": ["补贴", "购置税"]}},
            {"id": 2, "title": "只有标题", "facts": {}},
        ]}}
        text = _external_prompt(archive)
        self.assertTrue(text.startswith("【外部情报｜只可引用下列真实条目】"))
        self.assertIn("[外1] 购车补贴实施细则（2026 年最新版）", text)
        self.assertIn("1.2万元（value=12000.0）", text)
        self.assertIn("3000元（value=3000.0）", text)
        self.assertIn("3.5%（value=3.5）", text)
        self.assertIn("补贴", text)
        # 前三条之外的数字与空 raw 都不得进入提示词，避免模型引用未给出的数字
        self.assertNotIn("超限额", text)
        self.assertNotIn("（value=1）", text)
        self.assertIn("[外2] 只有标题 ｜ 无可量化要点", text)
        self.assertIn("external_use.cited", text)

    def test_archive_without_external_block(self):
        self.assertIn("未获取", _external_prompt({}))
        self.assertIn("未获取", _external_prompt(None))


class CrossBriefTest(unittest.TestCase):
    """看板/收尾共用的第 2 轮简报，只列定向回应轮次。"""

    def test_without_second_round_says_none(self):
        self.assertEqual(_cross_brief({}, {}), "【定向回应】无")
        self.assertEqual(_cross_brief(None, None), "【定向回应】无")

    def test_triggered_and_converged_is_reported(self):
        pro = {"cross_state": {"triggered": True, "converged": True}}
        self.assertIn("正方：已触发第 2 轮，收敛=是", _cross_brief(pro, {}))

    def test_triggered_without_converge_is_reported(self):
        con = {"cross_state": {"triggered": True, "converged": False}}
        self.assertIn("反方：已触发第 2 轮，收敛=否", _cross_brief({}, con))

    def test_only_respond_rounds_are_listed_in_order(self):
        pro = {"cross_state": {"triggered": True, "converged": False},
               "cross_rounds": [
                   {"round": 1, "source": "initial",
                    "responses": [{"target": "初稿条目", "verdict": "接受",
                                   "point": "不应出现在简报里"}]},
                   {"round": 2, "source": "respond",
                    "responses": [{"target": "反方风险二", "verdict": "限定",
                                   "point": "收窄成立条件"}]}]}
        con = {"cross_state": {"triggered": True, "converged": False},
               "cross_rounds": [
                   {"round": 2, "source": "respond",
                    "responses": [{"target": "正方论据一", "verdict": "维持",
                                   "point": "数字有档案支撑"}]}]}
        text = _cross_brief(pro, con)
        self.assertNotIn("初稿条目", text)
        self.assertNotIn("不应出现在简报里", text)
        self.assertIn("正方第 2 轮回应 反方风险二：限定（收窄成立条件）", text)
        self.assertIn("反方第 2 轮回应 正方论据一：维持（数字有档案支撑）", text)
        self.assertLess(text.index("正方第 2 轮回应"),
                        text.index("反方第 2 轮回应"))


class RespondRoundPromptTest(unittest.TestCase):
    """第 2 轮定向回应：提示词只放开被点名条目，契约越界则重试一次。"""

    ARCHIVE = {"question": "要不要买一台二十万级的家用车",
               "external": {"items": []}}
    OWN = {"arguments": [{"claim": "通勤更省时间", "basis": "单程 20 公里"}]}
    OPPONENT = {"risks": [{"desc": "现金流压力", "level": "中"}]}
    OWN_CROSS = {"side": "PRO", "cross": [
        {"target": "反方风险二", "verdict": "接受", "point": "该风险成立"}]}
    OPP_CROSS = {"side": "CON", "cross": [
        {"target": "正方论据一", "verdict": "反驳", "point": "高估收益"}]}

    def _respond(self, team, method, conflicts):
        return method(self.ARCHIVE, self.OWN, self.OPPONENT, self.OWN_CROSS,
                      self.OPP_CROSS, conflicts, 2)

    def test_pro_prompt_restricts_to_named_target(self):
        reply = {"side": "PRO", "responses": [
            {"target": "反方风险二", "verdict": "限定", "point": "收窄成立条件"}]}
        team = make_team([dumps(reply)], temps={"A2": 0.5})
        result = self._respond(team, team.respond_pro, ["反方风险二"])
        self.assertEqual(result["responses"][0]["target"], "反方风险二")
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A2")
        self.assertEqual(call["temperature"], 0.5)
        user = call["messages"][0]["content"]
        self.assertIn("你本轮论据", user)
        self.assertIn("你第 1 轮交叉质询", user)
        self.assertIn("反方针对你的质询", user)
        self.assertIn("第 2 轮只回应以下被点名条目：反方风险二。", user)
        self.assertIn("不得再发起 evidence_requests", user)
        self.assertIn("不得给最终结论", user)
        self.assertIn("要不要买一台二十万级的家用车", user)

    def test_con_prompt_uses_risk_wording(self):
        reply = {"side": "CON", "responses": [
            {"target": "正方论据一", "verdict": "维持", "point": "数字有档案支撑"}]}
        team = make_team([dumps(reply)], temps={"A3": 0.6})
        result = self._respond(team, team.respond_con, ["正方论据一"])
        self.assertEqual(result["side"], "CON")
        self.assertEqual(result["responses"][0]["verdict"], "维持")
        call = team.client.calls[0]
        self.assertEqual(call["system"], "SYS-A3")
        self.assertEqual(call["temperature"], 0.6)
        user = call["messages"][0]["content"]
        self.assertIn("你本轮风险", user)
        self.assertIn("正方针对你的质询", user)
        self.assertIn("第 2 轮只回应以下被点名条目：正方论据一。", user)
        self.assertIn("不得新增或改写其它风险", user)

    def test_out_of_scope_target_triggers_correction_then_passes(self):
        bad = {"side": "PRO", "responses": [
            {"target": "正方论据一", "verdict": "接受", "point": "越界回应"}]}
        good = {"side": "PRO", "responses": [
            {"target": "反方风险二", "verdict": "接受", "point": "风险成立"}]}
        team = make_team([dumps(bad), dumps(good)])
        result = self._respond(team, team.respond_pro, ["反方风险二"])
        self.assertEqual(len(team.client.calls), 2)
        self.assertEqual(result["responses"][0]["point"], "风险成立")
        retry = team.client.calls[1]["messages"][0]["content"]
        self.assertIn("【修正要求】", retry)
        self.assertIn("第 2 轮回应了未被点名的条目：正方论据一", retry)
        self.assertIn("第 2 轮只回应以下被点名条目：反方风险二。", retry)

    def test_repeated_out_of_scope_target_raises_contract_error(self):
        bad = {"side": "CON", "responses": [
            {"target": "虚构风险九", "verdict": "接受", "point": "越界"}]}
        team = make_team([dumps(bad), dumps(bad)])
        with self.assertRaises(ContractError) as ctx:
            self._respond(team, team.respond_con, ["正方论据一"])
        self.assertIn("第 2 轮回应了未被点名的条目：虚构风险九", str(ctx.exception))
        self.assertEqual(len(team.client.calls), 2)

    def test_external_items_are_offered_to_responder(self):
        archive = {"question": "要不要买车", "external": {"items": [
            {"id": 3, "title": "地方补贴", "facts": {"amounts": [
                {"raw": "8000元", "value": 8000.0}]}}]}}
        reply = {"side": "PRO", "responses": [
            {"target": "反方风险二", "verdict": "限定", "point": "按补贴后口径"}]}
        team = make_team([dumps(reply)])
        team.respond_pro(archive, self.OWN, self.OPPONENT, self.OWN_CROSS,
                         self.OPP_CROSS, ["反方风险二"], 2)
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("【外部情报｜只可引用下列真实条目】", user)
        self.assertIn("[外3] 地方补贴", user)
        self.assertIn("8000元（value=8000.0）", user)


class CrossDraftPromptTest(unittest.TestCase):
    """第 2 轮交叉质询定稿：证据回灌后只允许改 point / basis。"""

    def test_pro_draft_asks_for_final_cross_only(self):
        reply = {"side": "PRO", "cross": [
            {"target": "反方风险一", "verdict": "接受", "point": "证据支持",
             "basis": "外1"}]}
        team = make_team([dumps(reply)])
        result = team.cross_pro({"question": "要不要买车"},
                               {"arguments": [{"claim": "通勤更省时间"}]},
                               {"risks": [{"desc": "现金流压力"}]},
                               round_no=2, draft={"side": "PRO", "cross": []})
        self.assertEqual(result["cross"][0]["basis"], "外1")
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("你上一版交叉质询", user)
        self.assertIn("编排层已按你的 evidence_requests 回灌证据", user)
        self.assertIn("请只更新 point / basis", user)
        self.assertIn("最终交叉质询 JSON（第 2 轮）", user)
        self.assertNotIn("逐条点评反方三条风险", user)

    def test_con_draft_keeps_round_number(self):
        reply = {"side": "CON", "cross": [
            {"target": "正方论据一", "verdict": "反驳", "point": "高估收益",
             "basis": "外2"}]}
        team = make_team([dumps(reply)])
        team.cross_con({"question": "要不要买车"},
                       {"risks": [{"desc": "现金流压力"}]},
                       {"arguments": [{"claim": "通勤更省时间"}]},
                       round_no=3, draft={"side": "CON", "cross": []})
        user = team.client.calls[0]["messages"][0]["content"]
        self.assertIn("你上一版交叉质询", user)
        self.assertIn("不得新增或删除 cross 条目", user)
        self.assertIn("最终交叉质询 JSON（第 3 轮）", user)
        self.assertNotIn("逐条点评正方三条论据", user)


if __name__ == "__main__":
    unittest.main()