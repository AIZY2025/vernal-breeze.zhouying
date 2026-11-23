import json
import unittest

import _support  # noqa: F401

from core import contract
from core.agents import LLMTeam
from core.contract import ContractError
from core.mock import MockTeam, build_archive


class ContractAgainstMockTest(unittest.TestCase):
    def setUp(self):
        self.archive = build_archive(
            "我要不要买一台二十万级的家用车",
            "答：月收入1.15万，可动用存款120万，每月能存3000，最多能接受一次性支出20万")
        self.team = MockTeam()

    def test_a1_both_forms(self):
        contract.check_a1(self.team.a1_questions("我要不要买车", [], 0))
        contract.check_a1(self.team.a1_archive("我要不要买车", [], 3))
        contract.check_archive(self.team.a1_archive("我要不要买车", [], 3))

    def test_sides(self):
        contract.check_pro(self.team.judge_pro(self.archive, 1))
        contract.check_con(self.team.judge_con(self.archive, 1))
        contract.check_neu(self.team.judge_neu(self.archive, 1))

    def test_cross_examine(self):
        con = self.team.judge_con(self.archive, 1)
        pro = self.team.judge_pro(self.archive, 1)
        contract.check_cross(self.team.cross_pro(self.archive, pro, con, 1))
        contract.check_cross(self.team.cross_con(self.archive, con, pro, 1))

    def test_board_and_report(self):
        pro = self.team.judge_pro(self.archive, 1)
        con = self.team.judge_con(self.archive, 1)
        neu = self.team.judge_neu(self.archive, 1)
        contract.check_board(self.team.arbitrate(self.archive, pro, con, neu, 1, [], [], "测试"))
        contract.check_report(self.team.final_report(self.archive, pro, con, neu, 1, ""))


class ContractRejectTest(unittest.TestCase):
    def test_missing_archive(self):
        with self.assertRaises(ContractError):
            contract.check_archive({"kind": "archive"})

    def test_archive_missing_required_fields(self):
        with self.assertRaises(ContractError):
            contract.check_archive({"archive": {"question": "x"}})
        with self.assertRaises(ContractError):
            contract.check_archive({"archive": {
                "question": "x", "type": "消费支出", "window": "三个月",
                "completeness": "充足", "finance": {"cash": 100000},
                "options": [{"id": "A"}], "needs": [], "facts": [], "unknowns": []}})

    def test_a1_archive_kind_must_carry_archive(self):
        with self.assertRaises(ContractError):
            contract.check_a1({"kind": "archive"})

    def test_intel_use_must_be_object_when_present(self):
        with self.assertRaises(ContractError):
            contract.check_pro({"arguments": [{"claim": "有收益", "basis": "数据"}],
                                "intel_use": "无"})

    def test_report_missing_switch(self):
        with self.assertRaises(ContractError):
            contract.check_report({"report": {
                "verdict": "执行主方案", "reason": "安全边界未越界",
                "summary": ["a"], "compare": ["c"], "plans": ["p"], "fallback": ["f"]}})

    def test_empty_arguments(self):
        with self.assertRaises(ContractError):
            contract.check_pro({"arguments": []})

    def test_claim_without_basis(self):
        with self.assertRaises(ContractError):
            contract.check_pro({"arguments": [{"claim": "有收益"}]})

    def test_risks_without_level(self):
        with self.assertRaises(ContractError):
            contract.check_con({"risks": [{"desc": "成本高"}]})

    def test_cross_without_verdict(self):
        with self.assertRaises(ContractError):
            contract.check_cross({"side": "PRO", "cross": [
                {"target": "反方风险一", "point": "接受"}]})

    def test_cross_empty(self):
        with self.assertRaises(ContractError):
            contract.check_cross({"side": "CON", "cross": []})

    def test_cashflow_without_months(self):
        with self.assertRaises(ContractError):
            contract.check_neu({"cashflow": {}, "thresholds": [{}]})

    def test_board_without_lean(self):
        with self.assertRaises(ContractError):
            contract.check_board({"brief": ["a"], "dispute": ["b"], "conclusion": {}})

    def test_board_without_verdict(self):
        with self.assertRaises(ContractError):
            contract.check_board({"brief": ["a"], "dispute": ["b"],
                                  "conclusion": {"reason": "安全边界未越界"}})

    def test_board_rejects_vague_verdict(self):
        with self.assertRaises(ContractError):
            contract.check_board({"brief": ["a"], "dispute": ["b"],
                                  "conclusion": {"verdict": "视情况而定",
                                                 "reason": "还需观察"}})

    def test_board_without_reason(self):
        with self.assertRaises(ContractError):
            contract.check_board({"brief": ["a"], "dispute": ["b"],
                                  "conclusion": {"verdict": "暂缓"}})

    def test_report_missing_section(self):
        with self.assertRaises(ContractError):
            contract.check_report({"report": {"summary": ["a"]}})

    def test_report_without_verdict(self):
        with self.assertRaises(ContractError):
            contract.check_report({"report": {
                "summary": ["a"], "compare": ["c"], "plans": ["p"], "fallback": ["f"]}})


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, system, messages, temperature=0.5):
        self.calls.append(messages[-1]["content"])
        return self.replies.pop(0)


class RetryTest(unittest.TestCase):
    def test_retry_on_contract_error(self):
        client = FakeClient(['{"arguments": []}',
                             '{"arguments": [{"claim": "省时间", "basis": "通勤"}]}'])
        data = LLMTeam(client, {}).judge_pro({}, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("修正要求", client.calls[1])
        self.assertEqual(data["arguments"][0]["claim"], "省时间")

    def test_gives_up_after_two_tries(self):
        client = FakeClient(['{"arguments": []}', '{"arguments": []}'])
        with self.assertRaises(ContractError):
            LLMTeam(client, {}).judge_pro({}, 1)


class ReasonCiteTest(unittest.TestCase):
    def setUp(self):
        self.archive = {"external": {"items": [{"id": 1, "title": "政策"}]}}
        self.pro = {"intel_use": {"cited": [{"id": 1, "how": "论据"}]}}

    def test_reason_must_cite_when_sides_cited(self):
        with self.assertRaises(ContractError):
            contract.check_reason_cite({"reason": "量化结论支持暂缓"},
                                       self.archive, (self.pro, {}))
        contract.check_reason_cite({"reason": "补贴数据 [外1] 支持暂缓"},
                                   self.archive, (self.pro, {}))

    def test_no_side_citation_does_not_force_reason(self):
        contract.check_reason_cite({"reason": "量化结论支持暂缓"}, self.archive, ({}, {}))

    def test_fake_id_is_rejected(self):
        with self.assertRaises(ContractError):
            contract.check_reason_cite({"reason": "引用 [外9]"}, self.archive, (self.pro, {}))

    def test_ensure_fills_only_real_cited_ids(self):
        payload = {"reason": "量化结论支持暂缓"}
        filled = contract.ensure_reason_cite(payload, self.archive, (self.pro, {}))
        self.assertEqual(filled, [1])
        self.assertIn("[外1]", payload["reason"])
        contract.check_reason_cite(payload, self.archive, (self.pro, {}))

    def test_ensure_leaves_valid_reason_untouched(self):
        payload = {"reason": "补贴数据 [外1] 支持暂缓"}
        self.assertEqual(contract.ensure_reason_cite(payload, self.archive, (self.pro, {})), [])
        self.assertEqual(payload["reason"], "补贴数据 [外1] 支持暂缓")

    def test_ensure_skips_when_no_side_citation_or_invalid_id(self):
        payload = {"reason": "量化结论支持暂缓"}
        self.assertEqual(contract.ensure_reason_cite(payload, self.archive, ({}, {})), [])
        self.assertEqual(payload["reason"], "量化结论支持暂缓")
        fake_side = {"intel_use": {"cited": [{"id": 9}]}}
        self.assertEqual(contract.ensure_reason_cite(payload, self.archive, (fake_side, {})), [])
        self.assertEqual(payload["reason"], "量化结论支持暂缓")


class BoardCiteFillTest(unittest.TestCase):
    """真实模型漏标引用时，看板契约不得中止整场。"""

    def setUp(self):
        self.archive = {"external": {"items": [{"id": 1, "title": "政策"},
                                               {"id": 2, "title": "行情"}]}}
        self.pro = {"intel_use": {"cited": [{"id": 1}, {"id": 2}]}}

    def test_arbitrate_fills_reason_cite_without_retry(self):
        payload = {
            "brief": ["问题：要不要买", "正方：a", "反方：b", "量化：c"],
            "dispute": [{"point": "价格", "root": "事实差异", "detail": "样本不同"}],
            "conclusion": {"verdict": "暂缓", "reason": "量化结论支持暂缓",
                           "conditions": ["门槛"]},
            "session_closed": False,
        }
        client = FakeClient([json.dumps(payload, ensure_ascii=False)])
        data = LLMTeam(client, {}).arbitrate(
            self.archive, self.pro, {}, {}, 1, [], [], "初始研判")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(data["_cite_filled"], [1, 2])
        self.assertIn("[外1]", data["conclusion"]["reason"])


class RequestContractTest(unittest.TestCase):
    def test_a1_requests_must_have_query(self):
        with self.assertRaises(ContractError):
            contract.check_a1({"kind": "questions", "questions": ["q"],
                               "research_requests": [{"purpose": "缺 query"}]})
        contract.check_a1({"kind": "questions", "questions": ["q"],
                           "research_requests": [{"query": "有效查询"}]})

    def test_cross_and_neu_requests(self):
        with self.assertRaises(ContractError):
            contract.check_cross({"side": "PRO", "cross": [
                {"target": "t", "verdict": "接受", "point": "p"}],
                "evidence_requests": "bad"})
        contract.check_neu({
            "cashflow": {"months": 1},
            "thresholds": [{"name": "x", "value": "y"}],
            "evidence_requests": [{"query": "有效查询"}]})


class RespondContractTest(unittest.TestCase):
    def test_only_named_targets_allowed(self):
        with self.assertRaises(ContractError):
            contract.check_respond({"side": "PRO", "responses": [
                {"target": "正方论据一", "verdict": "维持", "point": "p"}]},
                ["正方论据二"])
        contract.check_respond({"side": "PRO", "responses": [
            {"target": "正方论据二", "verdict": "限定", "point": "p"}]},
            ["正方论据二"])

    def test_invalid_verdict_is_rejected(self):
        with self.assertRaises(ContractError):
            contract.check_respond({"side": "CON", "responses": [
                {"target": "反方风险一", "verdict": "坚持", "point": "p"}]},
                ["反方风险一"])


if __name__ == "__main__":
    unittest.main()