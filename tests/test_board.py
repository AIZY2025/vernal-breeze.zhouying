import copy
import unittest

import _support  # noqa: F401

from core.board import external_usage_text, render_archive, render_board, render_report
from core.mock import MockTeam, build_archive


class BoardRenderTest(unittest.TestCase):
    def setUp(self):
        self.archive = build_archive(
            "我要不要买一台二十万级的家用车",
            "答：月收入1.15万，可动用存款120万，每月能存3000，最多能接受一次性支出20万")
        team = MockTeam()
        self.pro = team.judge_pro(self.archive, 1)
        self.con = team.judge_con(self.archive, 1)
        self.neu = team.judge_neu(self.archive, 1)

    def test_archive_marker(self):
        text = render_archive(self.archive)
        self.assertIn("_BACKGROUND_READY=true", text)
        self.assertIn("《决策背景档案》", text)

    def test_board_sections(self):
        payload = MockTeam().arbitrate(self.archive, self.pro, self.con, self.neu, 1, [], [], "测试")
        text = render_board(version=1, archive=self.archive, pro=self.pro, con=self.con,
                            neu=self.neu, brief=payload["brief"], dispute=payload["dispute"],
                            conclusion=payload["conclusion"], log=["测试"], intent_note="测试")
        for section in ("【速览四行】", "一、决策问题与背景摘要", "二、正方核心论据",
                        "三、反方核心风险", "四、量化关键结论", "五、争议焦点",
                        "六、可执行交互项", "七、仲裁判定"):
            self.assertIn(section, text)
        self.assertIn("《沙盘看板》V1", text)
        self.assertIn("【仲裁判定】", text)
        self.assertIn("判定：", text)
        self.assertIn("理由：", text)

    def test_report_marker(self):
        payload = MockTeam().final_report(self.archive, self.pro, self.con, self.neu, 1, "")
        text = render_report(payload["report"], {"title": "｜ V1 ｜ 测试"})
        self.assertIn("_SESSION=CLOSED", text)
        self.assertIn("六、风险兜底建议", text)

    def test_empty_sides_do_not_crash(self):
        text = render_board(version=1, archive={}, pro={}, con={}, neu={})
        self.assertIn("（正方未返回）", text)
        self.assertIn("（反方未返回）", text)

    def test_missing_finance_is_marked_as_assumption(self):
        text = render_board(version=1, archive={}, pro={}, con={}, neu={})
        self.assertIn("月收入 未提供（按假设值 1.15万元）", text)
        self.assertIn("可动用存款 未提供（按假设值 120万元）", text)

    def test_unfetched_intel_is_shown_on_board(self):
        archive = dict(self.archive, external={
            "status": "未获取", "reason": "未配置搜索凭据", "items": []})
        text = render_board(version=1, archive=archive, pro=self.pro, con=self.con, neu=self.neu)
        self.assertIn("外部情报：未获取 ｜ 未配置搜索凭据", text)

    def test_external_citations_show_who_used_which_item(self):
        external = {"status": "已获取", "provider": "元宝搜索",
                    "retrieved_at": "2026-11-21 10:00:00", "items": [
                        {"id": 1, "title": "行业观察", "url": "https://e.com/1",
                         "date": "2026-10-01", "site": "示例网", "excerpt": "需求变化"},
                        {"id": 2, "title": "价格走势", "url": "https://e.com/2",
                         "date": "2026-10-02", "site": "示例报", "excerpt": "价格回落"},
                        {"id": 3, "title": "购房流程", "url": "https://e.com/3",
                         "date": "2026-10-03", "site": "示例站", "excerpt": "流程科普"}]}
        archive = dict(self.archive, external=external)
        pro = copy.deepcopy(self.pro)
        con = copy.deepcopy(self.con)
        pro["arguments"][0]["basis"] += " [外1]"
        con["risks"][0]["level_basis"] += " [外2]"
        pro["intel_use"] = {"cited": [{"id": 1, "how": "论据一的行业数据"}],
                            "skipped": "条目二与正方论据无直接关系"}
        payload = MockTeam().arbitrate(archive, pro, con, self.neu, 1, [], [], "测试")
        text = render_board(version=1, archive=archive, pro=pro, con=con, neu=self.neu,
                            brief=payload["brief"], dispute=payload["dispute"],
                            conclusion=payload["conclusion"])
        self.assertIn("引用：[外1] → 正方论据一", text)
        self.assertIn("[外2] → 反方风险一", text)
        self.assertIn("正方说明：条目二与正方论据无直接关系", text)
        self.assertIn("未引用", text)

    def test_no_citation_is_stated_plainly(self):
        external = {"status": "已获取", "provider": "元宝搜索",
                    "retrieved_at": "2026-11-21 10:00:00", "items": [
                        {"id": 1, "title": "买房流程", "url": "https://e.com/1",
                         "date": "2026-10-01", "site": "示例网", "excerpt": "流程科普"}]}
        archive = dict(self.archive, external=external)
        text = render_board(version=1, archive=archive, pro=self.pro, con=self.con, neu=self.neu)
        self.assertIn("引用：无（三方均未把检索结果作为论据依据）", text)

    def test_stale_sides_are_marked_after_param_change(self):
        text = render_board(version=2, archive=self.archive, pro=self.pro, con=self.con,
                            neu=self.neu, stale_sides=True)
        self.assertIn("二、正方核心论据（数字为参数变更前口径）", text)
        self.assertIn("三、反方核心风险（数字为参数变更前口径）", text)

    def test_zero_burn_is_shown_as_not_applicable(self):
        archive = {"finance": {"monthly_income": 3000, "monthly_saving": 3000,
                               "cash": 100000, "max_one_time": 10000,
                               "monthly_delta": 0}}
        neu = MockTeam().judge_neu(archive, 1)
        text = render_board(version=1, archive=archive, pro=self.pro, con=self.con, neu=neu)
        self.assertIn("月消耗为 0，可支撑月数不适用", text)
        self.assertNotIn("可支撑 0.0 个月", text)

    def test_insufficient_finance_is_flagged_on_board(self):
        archive = {"question": "要不要结婚", "finance": {"monthly_income": 10000}}
        neu = MockTeam().judge_neu(archive, 1)
        text = render_board(version=1, archive=archive, pro={}, con={}, neu=neu)
        self.assertIn("数据说明：财务四项缺失两项及以上", text)
        self.assertIn("（财务缺口，按假设值估算）", text)

    def test_report_shows_verdict_and_external_note(self):
        payload = MockTeam().final_report(self.archive, self.pro, self.con, self.neu, 1, "")
        text = render_report(payload["report"], {
            "title": "｜ V1 ｜ 测试", "external_note": "引用：无（三方均未把检索结果作为论据依据）"})
        self.assertIn("【仲裁判定】", text)
        self.assertIn("附、外部情报使用", text)
        self.assertIn("引用：无", text)


class CiteAuditTest(unittest.TestCase):
    def setUp(self):
        self.archive = {"external": {"items": [
            {"id": 1, "title": "购车成本", "excerpt": "月供 1650 元",
             "facts": {"amounts": [{"value": 1650.0, "raw": "1650元"}]}}]}}

    def test_citation_matching_numbers_is_strong(self):
        pro = {"arguments": [{"claim": "成本可控", "basis": "月供 1650 元 [外1]",
                              "premise": "收入稳定"}]}
        text = external_usage_text(self.archive, pro, {})
        self.assertIn("[外1] → 正方论据一", text)
        self.assertNotIn("弱引用", text)

    def test_label_without_matching_fact_is_marked_weak(self):
        con = {"risks": [{"desc": "风险", "trigger": "x", "level": "中",
                          "level_basis": "参见 [外1]"}]}
        text = external_usage_text(self.archive, {}, con)
        self.assertIn("弱引用：未命中条目数字/实体", text)


if __name__ == "__main__":
    unittest.main()