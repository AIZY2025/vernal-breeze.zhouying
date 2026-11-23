import json
import tempfile
import time
import types
import unittest
from pathlib import Path

import _support  # noqa: F401

from web import server

QUERY = "我要不要买一台二十万级的家用车"
ANSWERS = [
    "三个月内要定下来，最晚春节前提车",
    "月收入大概1.15万，可动用存款120万，每月能存3000左右，最多能接受一次性支出20万",
    "除了买，也在考虑继续打车加周末租车，或者先暂缓",
]
CLOSE = "结束沙盘，给我结论"


def answer_when_ready(session, text, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline and not session.bus.awaiting():
        if not session.snapshot()["active"]:
            break
        time.sleep(0.02)
    session.answer(text)


def wait_session(session, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = session.snapshot()
        if snapshot.get("run_id") and not snapshot["active"]:
            return snapshot
        time.sleep(0.05)
    return session.snapshot()


def run_session(session, answers=ANSWERS, close=CLOSE):
    session.start(QUERY, "mock")
    for answer in answers:
        answer_when_ready(session, answer)
    answer_when_ready(session, close)
    return wait_session(session)


class GuardTest(unittest.TestCase):
    def _handler(self, headers, allowed=None, client=None, allow_remote=False):
        handler = types.SimpleNamespace()
        handler.headers = headers
        handler.client_address = client or ("127.0.0.1", 5000)
        handler._loopback_client = types.MethodType(server.Handler._loopback_client, handler)
        handler._host_name = server.Handler._host_name
        handler._fetch_site = types.MethodType(server.Handler._fetch_site, handler)
        handler._deny = types.MethodType(server.Handler._deny, handler)
        handler.app = types.SimpleNamespace(
            allowed_hosts=allowed or {"127.0.0.1", "localhost", "::1"},
            allow_remote=allow_remote)
        self.calls = []
        handler._json = lambda obj, code=200: self.calls.append((obj, code))
        return handler

    def test_local_host_allowed(self):
        handler = self._handler({"Host": "127.0.0.1:8765"})
        self.assertTrue(server.Handler._guard(handler))
        self.assertEqual(self.calls, [])

    def test_ipv6_host_allowed(self):
        self.assertTrue(server.Handler._guard(self._handler({"Host": "[::1]:8765"})))

    def test_foreign_host_blocked(self):
        handler = self._handler({"Host": "evil.example.com"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_foreign_origin_blocked(self):
        handler = self._handler({"Host": "127.0.0.1:8765",
                                 "Origin": "http://evil.example.com"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_local_origin_allowed(self):
        handler = self._handler({"Host": "127.0.0.1:8765",
                                 "Origin": "http://127.0.0.1:8765"})
        self.assertTrue(server.Handler._guard(handler))

    def test_custom_bind_host_allowed(self):
        handler = self._handler({"Host": "192.168.1.20:8765"}, allowed={"192.168.1.20"})
        self.assertTrue(server.Handler._guard(handler))

    def test_missing_host_blocked(self):
        handler = self._handler({})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_remote_client_blocked_by_default(self):
        handler = self._handler({"Host": "127.0.0.1:8765"}, client=("192.168.1.9", 4000))
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_remote_client_allowed_with_flag(self):
        handler = self._handler({"Host": "127.0.0.1:8765"}, client=("192.168.1.9", 4000),
                                 allow_remote=True)
        self.assertTrue(server.Handler._guard(handler))

    def test_bare_ipv6_host_allowed(self):
        self.assertTrue(server.Handler._guard(self._handler({"Host": "::1"})))

    def test_non_numeric_host_port_blocked(self):
        handler = self._handler({"Host": "127.0.0.1:evil"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_deny_closes_connection(self):
        handler = self._handler({"Host": "evil.example.com"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertTrue(handler.close_connection)

    def test_cross_site_fetch_blocked(self):
        handler = self._handler({"Host": "127.0.0.1:8765", "Sec-Fetch-Site": "cross-site"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_null_origin_blocked(self):
        handler = self._handler({"Host": "127.0.0.1:8765", "Origin": "null"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def test_null_origin_with_none_fetch_site_blocked(self):
        handler = self._handler({"Host": "127.0.0.1:8765", "Origin": "null",
                                 "Sec-Fetch-Site": "none"})
        self.assertFalse(server.Handler._guard(handler))
        self.assertEqual(self.calls[-1][1], 403)

    def _write_handler(self, headers):
        handler = self._handler(headers)
        handler._guard_write = types.MethodType(server.Handler._guard_write, handler)
        return handler

    def test_write_requires_json_content_type(self):
        handler = self._write_handler({"Host": "127.0.0.1:8765",
                                       "Content-Type": "text/plain"})
        self.assertFalse(handler._guard_write())
        self.assertEqual(self.calls[-1][1], 415)

    def test_write_allows_json_with_charset(self):
        handler = self._write_handler({"Host": "127.0.0.1:8765",
                                       "Content-Type": "application/json; charset=utf-8"})
        self.assertTrue(handler._guard_write())
        self.assertEqual(self.calls, [])

    def test_write_rejects_none_fetch_site(self):
        handler = self._write_handler({"Host": "127.0.0.1:8765",
                                       "Content-Type": "application/json",
                                       "Sec-Fetch-Site": "none"})
        self.assertFalse(handler._guard_write())
        self.assertEqual(self.calls[-1][1], 403)

    def test_write_allows_same_origin(self):
        handler = self._write_handler({"Host": "127.0.0.1:8765",
                                       "Content-Type": "application/json",
                                       "Sec-Fetch-Site": "same-origin"})
        self.assertTrue(handler._guard_write())

    def test_security_headers_are_sent(self):
        handler = types.SimpleNamespace()
        sent = []
        handler.send_header = lambda name, value: sent.append((name, value))
        server.Handler._security_headers(handler)
        names = dict(sent)
        self.assertIn("Content-Security-Policy", names)
        self.assertEqual(names["X-Content-Type-Options"], "nosniff")
        self.assertEqual(names["X-Frame-Options"], "DENY")


class SessionPersistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text('{"provider": "mock"}', encoding="utf-8")
        self.session = server.Session(self.tmp.name, cfg)

    def test_full_session_persists_transcript(self):
        snapshot = run_session(self.session)
        self.assertFalse(snapshot["active"])
        run_dir = self.session.root / "runs" / snapshot["run_id"]
        self.assertTrue((run_dir / "最终报告.txt").exists())
        self.assertTrue((run_dir / "transcript.md").exists())
        self.assertTrue((run_dir / "状态快照.json").exists())
        report = (run_dir / "最终报告.txt").read_text(encoding="utf-8")
        self.assertIn("_SESSION=CLOSED", report)
        meta = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(meta.get("通道"), "web")

    def test_only_final_show_carries_final_flag(self):
        run_session(self.session)
        events = self.session.bus.read(0, timeout=0.1)
        shows = [ev for ev in events if ev["type"] == "show"]
        self.assertEqual(len(shows), 3)
        self.assertFalse(shows[0].get("final"))
        self.assertFalse(shows[1].get("final"))
        self.assertTrue(shows[2].get("final"))
        self.assertIn("输出最终报告", shows[1]["text"])
        self.assertIn("_SESSION=CLOSED", shows[2]["text"])

    def test_busy_session_rejected(self):
        self.session.start(QUERY, "mock")
        with self.assertRaises(server.Busy):
            self.session.start(QUERY, "mock")
        for answer in ANSWERS:
            answer_when_ready(self.session, answer)
        answer_when_ready(self.session, CLOSE)
        self.assertFalse(wait_session(self.session)["active"])

    def test_answer_without_active_session_rejected(self):
        with self.assertRaises(server.NotAwaiting) as ctx:
            self.session.answer("空闲时作答")
        self.assertIn("已忽略", str(ctx.exception))
        users = [ev for ev in self.session.bus.read(0, timeout=0.01) if ev["type"] == "user"]
        self.assertEqual(users, [])
        self.assertFalse(self.session.bus.awaiting())

    def test_stop_active_session(self):
        self.session.start(QUERY, "mock")
        self.assertTrue(self.session.stop())
        snapshot = wait_session(self.session)
        self.assertFalse(snapshot["active"])
        run_dir = self.session.root / "runs" / snapshot["run_id"]
        self.assertFalse((run_dir / "最终报告.txt").exists())
        meta = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(meta.get("状态"), "已停止")
        dones = [ev for ev in self.session.bus.read(0, timeout=0.1) if ev["type"] == "done"]
        self.assertTrue(dones)
        self.assertTrue(dones[-1].get("stopped"))
        self.assertFalse(self.session.stop())

    def test_stop_without_active_is_noop(self):
        self.assertFalse(self.session.stop())

    def test_restart_after_stop(self):
        self.session.start(QUERY, "mock")
        self.session.stop()
        wait_session(self.session)
        self.session.start(QUERY, "mock")
        self.assertTrue(self.session.snapshot()["active"])
        self.session.stop()
        wait_session(self.session)

    def test_ask_events_are_sequential(self):
        self.session.start(QUERY, "mock")
        since = 0
        asks = []
        deadline = time.time() + 10
        while len(asks) < 3 and time.time() < deadline:
            for event in self.session.bus.read(since, timeout=1):
                since = event["id"]
                if event["type"] == "ask":
                    asks.append(event["prompt"])
                    answer_when_ready(self.session, ANSWERS[0])
        self.assertEqual(len(asks), 3)
        self.assertTrue(all(asks))
        answer_when_ready(self.session, CLOSE)
        wait_session(self.session)

    def test_start_rejects_unknown_provider(self):
        with self.assertRaises(ValueError):
            self.session.start(QUERY, "unknown")
        self.assertFalse(self.session.snapshot()["active"])


class RunHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text('{"provider": "mock"}', encoding="utf-8")
        self.session = server.Session(self.tmp.name, cfg)

    def test_empty_history(self):
        self.assertEqual(self.session.list_runs(), [])

    def test_list_and_read_report(self):
        run_session(self.session)
        runs = self.session.list_runs()
        self.assertEqual(len(runs), 1)
        item = runs[0]
        self.assertTrue(item["has_report"])
        self.assertEqual(item["query"], QUERY)
        data = self.session.read_run(item["id"])
        self.assertEqual(data["kind"], "report")
        self.assertIn("_SESSION=CLOSED", data["text"])

    def test_reject_traversal(self):
        for bad in ("../runs", "20261121-000000-../../x", "", "abc"):
            with self.assertRaises(ValueError):
                self.session.read_run(bad)

    def test_missing_run(self):
        with self.assertRaises(ValueError):
            self.session.read_run("20990101-000000")

    def test_list_files_and_read_board_version(self):
        run_session(self.session)
        item = self.session.list_runs()[0]
        files = self.session.list_run_files(item["id"])
        kinds = [f["kind"] for f in files]
        self.assertIn("report", kinds)
        self.assertIn("transcript", kinds)
        board = next(f for f in files if f["kind"] == "board")
        data = self.session.read_run(item["id"], "board", board["version"])
        self.assertEqual(data["kind"], "board")
        self.assertIn("《沙盘看板》V%d" % board["version"], data["text"])

    def test_read_unknown_board_version(self):
        run_session(self.session)
        item = self.session.list_runs()[0]
        with self.assertRaises(ValueError):
            self.session.read_run(item["id"], "board", 999)

    def test_list_files_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            self.session.list_run_files("../runs")

    def test_export_contains_structured_result(self):
        run_session(self.session)
        item = self.session.list_runs()[0]
        data = self.session.export_run(item["id"])
        self.assertEqual(data["id"], item["id"])
        self.assertIsInstance(data["snapshot"], dict)
        self.assertIn("archive", data["snapshot"])
        self.assertIsInstance(data["report"], dict)
        self.assertGreaterEqual(len(data["boards"]), 1)
        self.assertIn("《沙盘看板》", data["boards"][0]["text"])
        self.assertIn("transcript", data)

    def test_export_rejects_bad_id(self):
        with self.assertRaises(ValueError):
            self.session.export_run("../runs")

    def test_read_intel_returns_usage(self):
        run_session(self.session)
        item = self.session.list_runs()[0]
        base = next((Path(self.tmp.name) / "runs").iterdir())
        intel = {"status": "已获取", "provider": "测试源",
                 "retrieved_at": "2026-11-21 10:00:00", "queries": ["买车"], "items": [
                     {"id": 1, "title": "购车成本", "url": "https://e.com/1",
                      "date": "2026-10-01", "site": "示例网", "excerpt": "月供 1650 元",
                      "facts": {"amounts": [{"value": 1650.0, "raw": "1650元"}]}}]}
        (base / "外部情报.json").write_text(
            json.dumps(intel, ensure_ascii=False), encoding="utf-8")
        snapshot = json.loads((base / "状态快照.json").read_text(encoding="utf-8"))
        snapshot["archive"] = dict(snapshot.get("archive") or {}, external=intel)
        snapshot["pro"] = {"arguments": [{"claim": "成本可控", "basis": "月供 1650 元 [外1]"}]}
        (base / "状态快照.json").write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        data = self.session.read_intel(item["id"])
        self.assertEqual(data["intel"]["items"][0]["id"], 1)
        self.assertIn("[外1]", data["usage"])

    def test_stats_event_emitted_after_run(self):
        run_session(self.session)
        events = self.session.bus.read(0, timeout=0.1)
        stats = [event for event in events if event["type"] == "stats"]
        self.assertTrue(stats)
        self.assertEqual(stats[-1].get("calls"), 0)
        self.assertIn("seconds", stats[-1])

    def test_review_list_and_mark(self):
        snapshot = run_session(self.session)
        reviews = self.session.list_reviews()
        self.assertEqual(len(reviews), 1)
        item = reviews[0]
        self.assertEqual(item["id"], snapshot["run_id"])
        self.assertFalse(item["reviewed"])
        data = self.session.read_run(item["id"], "review")
        self.assertIn("due_at", data["text"])
        marked = self.session.review_run(item["id"])
        self.assertTrue(marked["reviewed_at"])
        self.assertTrue(self.session.list_reviews()[0]["reviewed"])

    def test_resume_completed_run_continues_version(self):
        snapshot = run_session(self.session)
        run_id = snapshot["run_id"]
        run_dir = self.session.root / "runs" / snapshot["run_id"]
        item = [row for row in self.session.list_runs() if row["id"] == run_id][0]
        self.assertTrue(item["resumable"])

        self.session.start_resume(run_id)
        since = 0
        answers = ["我把可动用存款改成150万", CLOSE]
        deadline = time.time() + 10
        while answers and time.time() < deadline:
            for event in self.session.bus.read(since, timeout=0.5):
                since = event["id"]
                if event["type"] == "ask":
                    answer_when_ready(self.session, answers.pop(0))
                    break
        wait_session(self.session)

        self.assertTrue((run_dir / "最终报告.txt").exists())
        self.assertTrue((run_dir / "看板_V2.txt").exists())
        transcript = (run_dir / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("续跑", transcript)
        meta = json.loads((run_dir / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(meta.get("续跑"), "是")


class InternalErrorMaskingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text('{"provider": "mock"}', encoding="utf-8")
        self.session = server.Session(self.tmp.name, cfg)

    def test_traceback_not_sent_to_client(self):
        original = server.build_team

        def boom(*args, **kwargs):
            raise RuntimeError("internal boom /private/path")

        server.build_team = boom
        self.addCleanup(setattr, server, "build_team", original)
        self.session.start(QUERY, "mock")
        deadline = time.time() + 10
        errors = []
        while time.time() < deadline and not errors:
            errors = [ev for ev in self.session.bus.read(0, timeout=0.2) if ev["type"] == "error"]
        self.assertTrue(errors)
        payload = json.dumps(errors, ensure_ascii=False)
        self.assertNotIn("Traceback", payload)
        self.assertNotIn("internal boom", payload)
        self.assertNotIn("/private/path", payload)
        self.assertNotIn("detail", errors[0])
        self.assertIn("内部错误", errors[0]["text"])


if __name__ == "__main__":
    unittest.main()