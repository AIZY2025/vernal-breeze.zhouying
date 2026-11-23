import json
import tempfile
import threading
import unittest
from pathlib import Path

import _support

from core.mock import MOCK_QUESTIONS, MockTeam
from core.orchestrator import Sandbox, SandboxCancelled, SandboxError, _history_text
from core.store import SessionStore

QUERY = "我要不要买一台二十万级的家用车"
BASE_ANSWERS = [
    "三个月内要定下来，最晚春节前提车",
    "月收入大概1.15万，可动用存款120万，每月能存3000左右，最多能接受一次性支出20万",
    "除了买，也在考虑继续打车加周末租车，或者先暂缓",
]
CLOSE = "结束沙盘，给我结论"


class SandboxFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, answers, team=None):
        ui = _support.FakeUI(answers)
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(team or MockTeam(), ui, store, demo=True)
        sandbox.run(QUERY)
        store.finish({"运行模式": "预置剧本"})
        return sandbox, ui, store.dir

    def test_demo_close_only(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE])
        self.assertEqual(sandbox.version, 1)
        self.assertEqual(sandbox.routes[-1]["intent"], "CLOSE")
        self.assertTrue((run_dir / "背景档案.json").exists())
        self.assertTrue((run_dir / "看板_V1.txt").exists())
        report = (run_dir / "最终报告.txt").read_text(encoding="utf-8")
        self.assertIn("_SESSION=CLOSED", report)
        self.assertIn("【仲裁判定】", report)
        self.assertTrue((run_dir / "transcript.md").exists())
        self.assertTrue((run_dir / "session.json").exists())
        checklist = json.loads((run_dir / "复查清单.json").read_text(encoding="utf-8"))
        self.assertIn("due_at", checklist)
        self.assertEqual(checklist["verdict"], "执行主方案")
        self.assertTrue(checklist["conditions"])
        self.assertEqual(checklist["reviewed_at"], "")
        shows = ui.of_kind("show")
        self.assertEqual(len(shows), 3)  # 档案 + 第一版看板 + 最终报告
        self.assertFalse(shows[0][2])
        self.assertFalse(shows[1][2])
        self.assertTrue(shows[2][2])
        self.assertIn("输出最终报告", shows[1][1])

    def test_challenge_then_param(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "反方说的养车成本每年两万太高了，我实际一年一万二",
            "我把可动用存款改成150万",
            CLOSE,
        ])
        self.assertEqual(sandbox.version, 3)
        self.assertEqual(len(sandbox.syncs), 1)
        self.assertTrue(any(item["side"] == "反方" for item in sandbox.drift))
        self.assertTrue(any(item["side"] == "量化" for item in sandbox.drift))
        self.assertTrue((run_dir / "看板_V2.txt").exists())
        self.assertTrue((run_dir / "看板_V3.txt").exists())
        self.assertEqual(sandbox.archive["finance"]["cash"], 1500000)

    def test_new_fact_rebuilds_archive(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "补充一个情况：公司明年搬到南山，通勤距离缩短一半",
            CLOSE,
        ])
        self.assertEqual(sandbox.version, 2)
        self.assertTrue(any("南山" in fact for fact in sandbox.archive["facts"]))

    def test_unchanged_revision_is_not_logged_as_drift(self):
        # 真实模型会给出 from=中 to=中 的“伪修订”，不应写进漂移日志或同步摘要
        class FlatTeam(MockTeam):
            def revise_con(self, archive, prev, user_text, round_no):
                res = super().revise_con(archive, prev, user_text, round_no)
                res["revision"] = {"item": "隐性持有成本", "from": "中", "to": "中",
                                   "basis": user_text}
                return res

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "反方说的养车成本每年两万太高了，我实际一年一万二",
            CLOSE,
        ], team=FlatTeam())
        self.assertFalse(any(item["side"] == "反方" for item in sandbox.drift))
        self.assertEqual(sandbox.syncs, [])

    def test_board_verdict_reason_uses_authority_months(self):
        class WrongMonthsTeam(MockTeam):
            def arbitrate(self, archive, pro, con, neu, version, drift, syncs, intent_note):
                payload = super().arbitrate(archive, pro, con, neu, version, drift, syncs,
                                            intent_note)
                payload["conclusion"]["reason"] = "可支撑999.0个月，安全边界未越界"
                return payload

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE], team=WrongMonthsTeam())
        board = (run_dir / "看板_V1.txt").read_text(encoding="utf-8")
        self.assertIn("【仲裁判定】", board)
        self.assertNotIn("999.0", board)
        self.assertIn("可支撑98.5 个月", board)

    def test_final_report_cannot_silently_flip_board_verdict(self):
        class DriftingTeam(MockTeam):
            def arbitrate(self, archive, pro, con, neu, version, drift, syncs, intent_note):
                payload = super().arbitrate(archive, pro, con, neu, version, drift, syncs,
                                            intent_note)
                payload["conclusion"] = {"verdict": "暂缓", "reason": "看板判定暂缓",
                                         "conditions": ["门槛"]}
                return payload

            def final_report(self, archive, pro, con, neu, version, rounds_text):
                payload = super().final_report(archive, pro, con, neu, version, rounds_text)
                payload["report"]["verdict"] = "执行主方案"
                payload["report"]["reason"] = "收尾时模型自行改判"
                return payload

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE], team=DriftingTeam())
        report = (run_dir / "最终报告.txt").read_text(encoding="utf-8")
        self.assertIn("【仲裁判定】暂缓", report)
        self.assertNotIn("【仲裁判定】执行主方案", report)
        self.assertIn("不静默改判", report)

    def test_final_report_allows_explicit_flip(self):
        class FlippingTeam(MockTeam):
            def arbitrate(self, archive, pro, con, neu, version, drift, syncs, intent_note):
                payload = super().arbitrate(archive, pro, con, neu, version, drift, syncs,
                                            intent_note)
                payload["conclusion"] = {"verdict": "暂缓", "reason": "看板判定暂缓",
                                         "conditions": ["门槛"]}
                return payload

            def final_report(self, archive, pro, con, neu, version, rounds_text):
                payload = super().final_report(archive, pro, con, neu, version, rounds_text)
                payload["report"]["verdict"] = "执行主方案"
                payload["report"]["reason"] = "改判：补充了新事实，收入确认下降"
                return payload

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE], team=FlippingTeam())
        report = (run_dir / "最终报告.txt").read_text(encoding="utf-8")
        self.assertIn("【仲裁判定】执行主方案", report)
        self.assertNotIn("不静默改判", report)

    def test_snapshot_persisted(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE])
        snapshot = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["运行模式"], "预置剧本")

    def test_new_fact_keeps_revised_cost_caliber(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "反方说的养车成本每年两万太高了，我实际一年一万二",
            "补充一个情况：公司明年搬到南山，通勤距离缩短一半",
            CLOSE,
        ])
        self.assertEqual(sandbox.version, 3)
        self.assertEqual(sandbox.archive["finance"]["monthly_delta"], 1000)
        self.assertEqual(sandbox.neu["cashflow"]["monthly_delta"], 1000)
        self.assertEqual(sandbox.neu["cashflow"]["months"], 105.3)
        board = (run_dir / "看板_V3.txt").read_text(encoding="utf-8")
        self.assertIn("月度增量 1,000", board)
        self.assertIn("可支撑 105.3 个月", board)
        report = (run_dir / "最终报告.txt").read_text(encoding="utf-8")
        self.assertIn("阈值二 1,000", report)

    def test_param_cost_caliber_survives_new_fact(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "我养车每年1.2万",
            "补充一个情况：公司搬到南山，通勤距离缩短一半",
            CLOSE,
        ])
        self.assertEqual(sandbox.version, 3)
        self.assertEqual(sandbox.archive["finance"]["monthly_delta"], 1000)
        self.assertEqual(sandbox.neu["cashflow"]["months"], 105.3)

    def test_invalid_param_is_rejected_without_writing_archive(self):
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "把月收入改成-100",
            "把月收入改成999999999",
            CLOSE,
        ])
        self.assertEqual(sandbox.version, 1)
        self.assertEqual(sandbox.archive["finance"]["monthly_income"], 11500)
        infos = " ".join(event[1] for event in ui.of_kind("info"))
        self.assertEqual(infos.count("参数值不合法"), 2)

    def test_cost_challenge_recomputes_without_reported_revision(self):
        # 成本口径质疑即使 A2 没给实质修订，也要按用户原话重算 A4 并写回档案
        class NoRevisionTeam(MockTeam):
            def revise_pro(self, archive, prev, user_text, round_no):
                res = super().revise_pro(archive, prev, user_text, round_no)
                res["revision"] = None
                return res

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [
            "质疑正方：收益被长期价值高估了，我实际每年养车成本一万二",
            CLOSE,
        ], team=NoRevisionTeam())
        self.assertEqual(sandbox.routes[0]["intent"], "CHALLENGE_PRO")
        self.assertEqual(sandbox.archive["finance"]["monthly_delta"], 1000)
        self.assertEqual(sandbox.neu["cashflow"]["monthly_delta"], 1000)
        delta_drift = [item for item in sandbox.drift
                       if item["side"] == "量化" and item["item"] == "月度增量支出"]
        self.assertTrue(delta_drift)
        self.assertEqual(delta_drift[-1]["basis"], "成本口径质疑后同步重算")
        self.assertIn("1,000 元/月", delta_drift[-1]["to"])

    def test_probe_archive_without_monthly_delta_is_backfilled(self):
        class MissingDeltaTeam(MockTeam):
            def a1_archive(self, query, history_text, asked_total, new_facts="", prev_archive=None):
                res = super().a1_archive(query, history_text, asked_total, new_facts, prev_archive)
                res["archive"].setdefault("finance", {}).pop("monthly_delta", None)
                return res

        answers = [
            BASE_ANSWERS[0],
            BASE_ANSWERS[1] + "，我每月养车1500元",
            BASE_ANSWERS[2],
        ]
        sandbox, ui, run_dir = self._run(answers + [CLOSE], team=MissingDeltaTeam())
        self.assertEqual(sandbox.archive["finance"]["monthly_delta"], 1500)
        self.assertEqual(sandbox.neu["cashflow"]["monthly_delta"], 1500)


class OrchestratorErrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_partial_judges_abort(self):
        class BrokenTeam(MockTeam):
            def judge_neu(self, archive, round_no=1, hint=""):
                raise RuntimeError("量化挂了")

        ui = _support.FakeUI(BASE_ANSWERS)
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(BrokenTeam(), ui, store, demo=True)
        with self.assertRaises(SandboxError):
            sandbox.run(QUERY)


class CancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_cancel_before_start_aborts_without_any_output(self):
        event = threading.Event()
        event.set()
        ui = _support.FakeUI([])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True, cancel_event=event)
        with self.assertRaises(SandboxCancelled):
            sandbox.run(QUERY)
        self.assertEqual(sandbox.close_reason, "user_stop")
        self.assertEqual(ui.of_kind("show"), [])
        self.assertFalse((store.dir / "最终报告.txt").exists())

    def test_cancel_mid_probe_stops_before_board(self):
        event = threading.Event()

        class StoppingUI(_support.FakeUI):
            def ask(self, prompt, echo=True):
                event.set()
                return "三个月内定下来"

        ui = StoppingUI()
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True, cancel_event=event)
        with self.assertRaises(SandboxCancelled):
            sandbox.run(QUERY)
        self.assertEqual(sandbox.asked_total, 0)
        self.assertEqual(ui.of_kind("show"), [])
        self.assertFalse((store.dir / "看板_V1.txt").exists())

    def test_stop_word_as_probe_answer_cancels_without_board(self):
        ui = _support.FakeUI(["停止推演"])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True)
        with self.assertRaises(SandboxCancelled):
            sandbox.run(QUERY)
        self.assertEqual(sandbox.close_reason, "user_stop")
        self.assertFalse((store.dir / "看板_V1.txt").exists())
        self.assertFalse((store.dir / "最终报告.txt").exists())

    def test_stop_route_keeps_board_but_skips_final_report(self):
        ui = _support.FakeUI(BASE_ANSWERS + ["停止推演"])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True)
        with self.assertRaises(SandboxCancelled):
            sandbox.run(QUERY)
        self.assertEqual(sandbox.routes[-1]["intent"], "STOP")
        self.assertEqual(sandbox.close_reason, "user_stop")
        self.assertTrue((store.dir / "看板_V1.txt").exists())
        self.assertFalse((store.dir / "最终报告.txt").exists())


class ProbeAndCrossTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _run(self, answers, team=None):
        ui = _support.FakeUI(answers)
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(team or MockTeam(), ui, store, demo=True)
        sandbox.run(QUERY)
        return sandbox, ui, store.dir

    def test_a1_asks_one_question_per_call_and_uses_last_answer(self):
        class RecordingTeam(MockTeam):
            def __init__(self):
                super().__init__()
                self.probe_calls = []

            def a1_questions(self, query, history_text, asked_total):
                self.probe_calls.append((asked_total, history_text))
                return super().a1_questions(query, history_text, asked_total)

        team = RecordingTeam()
        sandbox, ui, _ = self._run(BASE_ANSWERS + [CLOSE], team=team)
        self.assertEqual([call[0] for call in team.probe_calls], [0, 1, 2, 3])
        self.assertNotIn("问：", team.probe_calls[0][1])
        self.assertIn("问：" + MOCK_QUESTIONS[0], team.probe_calls[1][1])
        self.assertIn("答：" + BASE_ANSWERS[0], team.probe_calls[1][1])
        self.assertIn("问：" + MOCK_QUESTIONS[1], team.probe_calls[2][1])
        self.assertIn("答：" + BASE_ANSWERS[1], team.probe_calls[2][1])
        self.assertIn("问：" + MOCK_QUESTIONS[2], team.probe_calls[3][1])
        self.assertIn("答：" + BASE_ANSWERS[2], team.probe_calls[3][1])
        self.assertEqual(sandbox.asked_total, 3)

    def test_cross_examine_runs_once_after_first_judgement(self):
        class RecordingTeam(MockTeam):
            def __init__(self):
                super().__init__()
                self.cross_calls = []

            def cross_pro(self, archive, own, opponent, round_no=1):
                self.cross_calls.append(("PRO", round_no))
                return super().cross_pro(archive, own, opponent, round_no)

            def cross_con(self, archive, own, opponent, round_no=1):
                self.cross_calls.append(("CON", round_no))
                return super().cross_con(archive, own, opponent, round_no)

        team = RecordingTeam()
        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE], team=team)
        self.assertEqual(sorted(team.cross_calls), [("CON", 1), ("PRO", 1)])
        self.assertTrue(sandbox.pro.get("cross"))
        self.assertTrue(sandbox.con.get("cross"))
        tags = [event[2] for event in ui.of_kind("agent")
                if event[1] in ("A2 支持方", "A3 反对方")]
        self.assertIn("交叉质询", tags)
        self.assertIn("争议焦点", (run_dir / "看板_V1.txt").read_text(encoding="utf-8"))

    def test_cross_examine_failure_degrades_without_blocking_board(self):
        class BrokenCrossTeam(MockTeam):
            def cross_pro(self, archive, own, opponent, round_no=1):
                raise RuntimeError("质询挂了")

        sandbox, ui, run_dir = self._run(BASE_ANSWERS + [CLOSE], team=BrokenCrossTeam())
        self.assertEqual(sandbox.version, 1)
        self.assertTrue((run_dir / "看板_V1.txt").exists())
        self.assertTrue(sandbox.con.get("cross"))
        self.assertIsNone(sandbox.pro.get("cross"))
        self.assertTrue(any("交叉质询未完成" in event[1] for event in ui.of_kind("info")))

    def test_repeating_question_is_cut_off(self):
        class RepeatingTeam(MockTeam):
            def a1_questions(self, query, history_text, asked_total):
                return {"kind": "questions", "questions": ["1. 同一个问题？"]}

        sandbox, ui, _ = self._run([BASE_ANSWERS[0], BASE_ANSWERS[1], CLOSE], team=RepeatingTeam())
        self.assertEqual(sandbox.asked_total, 2)
        self.assertTrue(any("连续重复同一问题" in event[1] for event in ui.of_kind("info")))


class GateAndResearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_non_decision_never_reaches_agents(self):
        class NeverCalled(MockTeam):
            def a1_questions(self, *args, **kwargs):
                raise AssertionError("非决策问题不应进入 A1")

            def judge_pro(self, *args, **kwargs):
                raise AssertionError("非决策问题不应进入并行研判")

        ui = _support.FakeUI([])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(NeverCalled(), ui, store)
        sandbox.run("你好")
        self.assertTrue(sandbox.refused)
        self.assertEqual(sandbox.version, 0)
        self.assertFalse((store.dir / "看板_V1.txt").exists())
        self.assertTrue((store.dir / "入口判定.json").exists())

    def test_research_items_injected_into_archive_and_board(self):
        class FakeResearch:
            active = True
            max_items = 5

            def __init__(self):
                self.queries = []

            def _intel(self, query):
                return {"status": "已获取", "provider": "测试源",
                        "retrieved_at": "2026-11-21 10:00:00", "queries": [query],
                        "reason": "", "items": [
                            {"id": 1, "title": "行业观察", "url": "https://e.com/1",
                             "date": "2026-10-01", "site": "示例网", "score": 0.9,
                             "excerpt": "摘要"}]}

            def search_queries(self, queries, extra="", force=False,
                               max_items=None, terms_text=""):
                self.queries.append(queries[0])
                return self._intel(queries[0])

            def collect(self, query, extra="", kind="", archive=None, force=False):
                self.queries.append(query)
                return self._intel(query)

        research = FakeResearch()
        ui = _support.FakeUI(BASE_ANSWERS + [CLOSE])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True, research=research)
        sandbox.run(QUERY)
        self.assertEqual(len(sandbox.archive["external"]["items"]), 4)
        self.assertEqual(len(research.queries), 4)
        self.assertIn("车", research.queries[0])
        board = (store.dir / "看板_V1.txt").read_text(encoding="utf-8")
        self.assertIn("外部情报：4 条", board)
        self.assertIn("[外1]", board)
        self.assertIn("请求 A1", board)
        self.assertNotIn("引用：无（三方均未把检索结果作为论据依据）", board)
        self.assertTrue((store.dir / "外部情报.json").exists())
        self.assertIn("https://e.com/1",
                      (store.dir / "背景档案.json").read_text(encoding="utf-8"))

    def test_unfetched_research_is_visible_on_board(self):
        class DeadResearch:
            active = True

            def collect(self, query, extra="", kind="", archive=None, force=False):
                return {"status": "未获取", "provider": "", "retrieved_at": "",
                        "queries": [query], "reason": "未配置搜索凭据", "items": []}

        ui = _support.FakeUI(BASE_ANSWERS + [CLOSE])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True, research=DeadResearch())
        sandbox.run(QUERY)
        board = (store.dir / "看板_V1.txt").read_text(encoding="utf-8")
        self.assertIn("外部情报：未获取 ｜ 未配置搜索凭据", board)
        self.assertFalse((store.dir / "外部情报.json").exists())

    def test_challenge_with_research_hint_refreshes_intel(self):
        class CountingResearch:
            active = True
            max_items = 5

            def __init__(self):
                self.calls = []

            def _intel(self, query):
                return {"status": "已获取", "provider": "测试源",
                        "retrieved_at": "2026-11-21 10:00:00", "queries": [query],
                        "reason": "", "items": [
                            {"id": 1, "title": "购车补贴政策", "url": "https://e.com/1",
                             "date": "2026-10-01", "site": "示例网", "score": 0.9,
                             "excerpt": "补贴 1.2 万元",
                             "facts": {"amounts": [{"value": 12000.0, "raw": "1.2万元"}],
                                       "keywords": ["补贴"]}}]}

            def search_queries(self, queries, extra="", force=False,
                               max_items=None, terms_text=""):
                self.calls.append({"query": queries[0], "extra": extra, "force": force})
                return self._intel(queries[0])

            def collect(self, query, extra="", kind="", archive=None, force=False):
                self.calls.append({"query": query, "extra": extra, "force": force})
                return self._intel(query)

        research = CountingResearch()
        ui = _support.FakeUI(
            BASE_ANSWERS + ["质疑正方：月供太乐观了，按最新补贴政策是多少？", CLOSE])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True, research=research)
        sandbox.run(QUERY)
        self.assertEqual(len(research.calls), 5)
        self.assertTrue(research.calls[-1]["force"])
        self.assertIn("补贴", research.calls[-1]["extra"])


    def test_cross_second_round_triggers_on_conflict(self):
        ui = _support.FakeUI(BASE_ANSWERS + [CLOSE])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True)
        sandbox.run(QUERY)
        self.assertTrue(sandbox.pro["cross_state"]["triggered"])
        self.assertTrue(sandbox.con["cross_state"]["triggered"])
        self.assertFalse(sandbox.pro["cross_state"]["converged"])
        self.assertTrue(any(row.get("source") == "respond"
                            for row in sandbox.pro["cross_rounds"]))
        board = (store.dir / "看板_V1.txt").read_text(encoding="utf-8")
        self.assertIn("第 2 轮定向回应", board)
        self.assertIn("收敛：否", board)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_restore_and_resume_continue_version(self):
        ui = _support.FakeUI(BASE_ANSWERS + ["停止推演"])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True)
        with self.assertRaises(SandboxCancelled):
            sandbox.run(QUERY)
        snapshot = sandbox.snapshot("预置剧本", stopped=True)
        version = sandbox.version
        self.assertGreaterEqual(version, 1)

        store2 = SessionStore(Path(self.tmp.name), reuse=store.dir.name)
        ui2 = _support.FakeUI(["我把可动用存款改成150万", CLOSE])
        resumed = Sandbox(MockTeam(), ui2, store2, demo=True)
        resumed.restore(snapshot)
        self.assertEqual(resumed.version, version)
        self.assertEqual(resumed.query, QUERY)
        self.assertEqual(len(resumed.history_lines), len(sandbox.history_lines))
        resumed.resume()

        self.assertEqual(resumed.version, version + 1)
        self.assertEqual(resumed.archive["finance"]["cash"], 1500000)
        self.assertTrue((store2.dir / ("看板_V%d.txt" % (version + 1))).exists())
        self.assertEqual(resumed.close_reason, "user_close")

    def test_restore_rejects_empty_archive(self):
        ui = _support.FakeUI([])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(MockTeam(), ui, store, demo=True)
        with self.assertRaises(SandboxError):
            sandbox.restore({"version": 2})


class HistoryWindowTest(unittest.TestCase):
    def test_long_history_keeps_recent_lines(self):
        lines = ["第%02d行" % index + "内容" * 200 for index in range(40)]
        text = _history_text(lines, limit=2000)
        self.assertIn("已省略", text)
        self.assertIn(lines[-1], text)
        self.assertNotIn(lines[0], text)

    def test_short_history_is_unchanged(self):
        self.assertEqual(_history_text(["问：a", "答：b"]), "问：a\n答：b")


class _CountingTeam(MockTeam):
    """记录 A1 探询调用次数：粘档案入口必须完全跳过背景探询。"""

    def __init__(self):
        super().__init__()
        self.a1_calls = 0

    def a1_questions(self, *args, **kwargs):
        self.a1_calls += 1
        return super().a1_questions(*args, **kwargs)

    def a1_archive(self, *args, **kwargs):
        self.a1_calls += 1
        return super().a1_archive(*args, **kwargs)


class PasteArchiveTest(unittest.TestCase):
    """降级预案：直接载入外部档案，跳过 A1 探询进入并行研判。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = json.loads(
            (_support.ROOT / "sample_archive.json").read_text(encoding="utf-8"))

    def _sandbox(self, answers=None, team=None, cancel_event=None):
        ui = _support.FakeUI(answers if answers is not None else [CLOSE])
        store = SessionStore(Path(self.tmp.name))
        sandbox = Sandbox(team or MockTeam(), ui, store, demo=True,
                          cancel_event=cancel_event)
        return sandbox, ui, store

    def test_paste_skips_probe_and_builds_board(self):
        team = _CountingTeam()
        sandbox, ui, store = self._sandbox(team=team)
        sandbox.run_paste(self.archive)
        self.assertEqual(team.a1_calls, 0)
        self.assertEqual(sandbox.asked_total, 0)
        self.assertEqual(sandbox.history_lines, [])
        self.assertEqual(sandbox.version, 1)
        self.assertEqual(sandbox.query, self.archive["question"])
        self.assertEqual(sandbox.topic, self.archive["question"])
        self.assertEqual(sandbox.close_reason, "user_close")
        self.assertEqual(sandbox.routes[-1]["intent"], "CLOSE")

        saved = json.loads(
            (store.dir / "背景档案.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["finance"]["cash"], self.archive["finance"]["cash"])
        self.assertEqual(saved["options"], self.archive["options"])

        board = (store.dir / "看板_V1.txt").read_text(encoding="utf-8")
        self.assertIn("《沙盘看板》V1", board)
        self.assertIn("【本版变更】", board)
        self.assertIn("外部档案载入，三方并行研判", board)
        self.assertIn("· 路由：降级预案·直接推演", board)

        self.assertTrue((store.dir / "最终报告.txt").exists())
        store.finish({"运行模式": "预置剧本"})
        transcript = (store.dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("载入外部档案，跳过 A1", transcript)
        infos = [event[1] for event in ui.of_kind("info")]
        self.assertTrue(any("已载入外部档案，跳过 A1" in text for text in infos))
        judged = [event[1] for event in ui.of_kind("agent")
                  if event[2] == "研判完成"]
        self.assertEqual(sorted(judged), ["A2 支持方", "A3 反对方", "A4 量化测算"])

    def test_question_falls_back_to_argument(self):
        archive = {key: value for key, value in self.archive.items()
                   if key != "question"}
        sandbox, ui, store = self._sandbox()
        sandbox.run_paste(archive, "要不要换一台更省油的车")
        self.assertEqual(sandbox.query, "要不要换一台更省油的车")
        self.assertEqual(sandbox.topic, "要不要换一台更省油的车")
        self.assertEqual(sandbox.version, 1)
        self.assertTrue((store.dir / "看板_V1.txt").exists())

    def test_rejects_archive_that_is_not_object(self):
        sandbox, ui, store = self._sandbox()
        with self.assertRaises(SandboxError) as ctx:
            sandbox.run_paste(["not", "an", "archive"])
        self.assertEqual(str(ctx.exception), "载入的档案必须是 JSON 对象")
        with self.assertRaises(SandboxError):
            sandbox.run_paste({})
        self.assertEqual(ui.events, [])
        self.assertEqual(sandbox.archive, {})
        self.assertEqual(sandbox.version, 0)
        self.assertFalse((store.dir / "看板_V1.txt").exists())

    def test_cancel_event_aborts_before_writing(self):
        event = threading.Event()
        event.set()
        sandbox, ui, store = self._sandbox(cancel_event=event)
        with self.assertRaises(SandboxCancelled):
            sandbox.run_paste(self.archive)
        self.assertEqual(sandbox.close_reason, "user_stop")
        self.assertEqual(sandbox.archive, {})
        self.assertEqual(sandbox.version, 0)
        self.assertEqual(ui.events, [])
        self.assertFalse((store.dir / "背景档案.json").exists())
        self.assertFalse((store.dir / "看板_V1.txt").exists())


class VerdictLockTest(unittest.TestCase):
    """收尾判定锁定：没有新事实时，报告判定必须沿用最近一版看板。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sandbox = Sandbox(MockTeam(), _support.FakeUI([]),
                               SessionStore(Path(self.tmp.name)), demo=True)
        self.sandbox.version = 3

    def test_keeps_report_verdict_when_board_has_none(self):
        self.sandbox.conclusion = {}
        report = {"verdict": "暂缓", "reason": "等待新报价"}
        self.sandbox._lock_report_verdict(report)
        self.assertEqual(report["verdict"], "暂缓")
        self.assertEqual(report["reason"], "等待新报价")

    def test_fills_missing_report_verdict_from_board(self):
        self.sandbox.conclusion = {"verdict": "执行主方案"}
        report = {"verdict": "", "reason": "量化口径支持"}
        self.sandbox._lock_report_verdict(report)
        self.assertEqual(report["verdict"], "执行主方案")
        self.assertEqual(report["reason"], "量化口径支持")

    def test_same_verdict_keeps_original_reason(self):
        self.sandbox.conclusion = {"verdict": "暂缓"}
        report = {"verdict": "暂缓", "reason": "门槛未达标"}
        self.sandbox._lock_report_verdict(report)
        self.assertEqual(report["verdict"], "暂缓")
        self.assertEqual(report["reason"], "门槛未达标")

    def test_flip_is_overridden_with_board_verdict(self):
        self.sandbox.conclusion = {"verdict": "暂缓"}
        report = {"verdict": "执行主方案", "reason": "模型自行改判"}
        self.sandbox._lock_report_verdict(report)
        self.assertEqual(report["verdict"], "暂缓")
        self.assertIn("沿用最近看板 V3 判定", report["reason"])
        self.assertIn("模型自行改判", report["reason"])

    def test_explicit_flip_reason_is_respected(self):
        self.sandbox.conclusion = {"verdict": "暂缓"}
        report = {"verdict": "执行主方案", "reason": "改判：补充了新事实，收入确认下降"}
        self.sandbox._lock_report_verdict(report)
        self.assertEqual(report["verdict"], "执行主方案")
        self.assertEqual(report["reason"], "改判：补充了新事实，收入确认下降")


class CrossRespondFallbackTest(unittest.TestCase):
    """第 2 轮定向回应的降级：能力缺失或调用失败时保留第 1 轮结果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ui = _support.FakeUI([])
        self.store = SessionStore(Path(self.tmp.name))
        self.sandbox = Sandbox(MockTeam(), self.ui, self.store, demo=True)
        self.sandbox.pro = {"arguments": [{"claim": "通勤更省时间"}]}
        self.sandbox.con = {"risks": [{"desc": "现金流压力", "level": "高"}]}
        self.drafts = {"A2": {"side": "PRO", "cross": []},
                       "A3": {"side": "CON", "cross": []}}

    def test_team_without_responder_is_skipped(self):
        class PlainTeam:
            pass

        self.sandbox.team = PlainTeam()
        result = self.sandbox._cross_respond(
            1, self.drafts, {"pro": ["正方论据一"], "con": []})
        self.assertEqual(result, {})
        self.assertNotIn("cross_rounds", self.sandbox.pro)
        self.assertEqual(self.ui.events, [])

    def test_failure_keeps_first_round_and_reports(self):
        class FailingTeam(MockTeam):
            def respond_pro(self, *args, **kwargs):
                raise RuntimeError("模型超时")

        self.sandbox.team = FailingTeam()
        result = self.sandbox._cross_respond(
            1, self.drafts, {"pro": ["正方论据一"], "con": []})
        self.assertEqual(result, {})
        self.assertEqual(self.sandbox.pro.get("cross_rounds", []), [])
        infos = [event[1] for event in self.ui.of_kind("info")]
        self.assertTrue(any("第 2 轮定向回应未完成" in text and "模型超时" in text
                            for text in infos))
        self.sandbox.store.finish({})
        transcript = (self.store.dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("定向回应", transcript)
        self.assertIn("模型超时", transcript)


if __name__ == "__main__":
    unittest.main()