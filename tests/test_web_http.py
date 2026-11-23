"""Web 层 HTTP 端到端：chunked SSE 帧、回放、回环/Host/Origin 防护与接口边界。

与 test_web_server.py 的区别：这里真的起 ThreadingHTTPServer 走 HTTP，
验证线上浏览器实际拿到的东西（chunked 编码、SSE 帧、响应头），而不只是 Session 对象。
"""
import http.client
import json
import re
import socket
import tempfile
import threading
import time
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import _support  # noqa: F401

from web import server


class HttpLayerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Path(cls.tmp.name) / "config.json"
        cfg.write_text('{"provider": "mock"}', encoding="utf-8")
        cls.original_app = server.Handler.app
        server.Handler.app = types.SimpleNamespace(
            session=server.Session(cls.tmp.name, cfg),
            allowed_hosts={"127.0.0.1", "localhost", "::1"},
            allow_remote=False)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.session = server.Handler.app.session
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                cls._request(cls, "GET", "/api/status")
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("测试服务没有在 5 秒内就绪")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server.Handler.app = cls.original_app
        cls.tmp.cleanup()

    def setUp(self):
        self.session.reset()

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = None
        hdrs = {"Host": "127.0.0.1:%d" % self.port}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        result = (resp.status, data, {k.lower(): v for k, v in resp.getheaders()})
        conn.close()
        return result

    def _raw_stream(self, since=0, timeout=5):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        sock.sendall(("GET /api/stream?since=%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                      "Accept: text/event-stream\r\n\r\n" % (since, self.port)).encode())
        raw = b""
        try:
            while True:
                block = sock.recv(4096)
                if not block:
                    break
                raw += block
                head, _, body = raw.partition(b"\r\n\r\n")
                if head and body and re.match(rb"^[0-9A-F]+\r\n", body):
                    break
        except socket.timeout:
            pass
        sock.close()
        return raw

    # ------------------------------------------------------------ 静态与防护
    def test_static_page_carries_security_headers(self):
        status, data, headers = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("春风", data.decode("utf-8"))
        self.assertIn("script-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        status, _, headers = self._request("GET", "/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])

    def test_foreign_host_and_origin_rejected(self):
        status, _, _ = self._request("GET", "/api/status", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self._request("GET", "/api/status",
                                     headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self._request("GET", "/api/status",
                                     headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_local_origin_allowed(self):
        status, data, _ = self._request("GET", "/api/status",
                                        headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["active"])

    def test_status_hides_server_version_and_absolute_path(self):
        status, data, headers = self._request("GET", "/api/status")
        self.assertEqual(status, 200)
        server_header = headers.get("server", "")
        self.assertNotIn("Python/", server_header)
        self.assertNotIn("DecisionSandbox", server_header)
        body = json.loads(data.decode("utf-8"))
        self.assertNotIn("run_dir", body)
        self.assertNotIn(str(self.session.root), data.decode("utf-8"))

    def test_run_id_traversal_rejected(self):
        status, _, _ = self._request("GET", "/api/runs/..%2F..%2Fcore")
        self.assertEqual(status, 404)

    def _fake_run(self):
        base = Path(self.tmp.name) / "runs" / "20200101-000001"
        base.mkdir(parents=True, exist_ok=True)
        (base / "看板_V1.txt").write_text("《沙盘看板》V1\n【仲裁判定】执行主方案",
                                        encoding="utf-8")
        (base / "看板_V2.txt").write_text("《沙盘看板》V2\n【仲裁判定】暂缓",
                                        encoding="utf-8")
        (base / "最终报告.txt").write_text("_SESSION=CLOSED", encoding="utf-8")
        (base / "transcript.md").write_text("# 记录", encoding="utf-8")
        return base

    def test_run_files_and_board_version_over_http(self):
        self._fake_run()
        status, data, _ = self._request("GET", "/api/runs/20200101-000001/files")
        self.assertEqual(status, 200)
        files = json.loads(data)["files"]
        self.assertEqual([f["kind"] for f in files][:2], ["report", "board"])
        board = next(f for f in files if f["kind"] == "board")
        self.assertEqual(board["version"], 2)
        status, data, _ = self._request("GET",
                                        "/api/runs/20200101-000001?kind=board&version=1")
        self.assertEqual(status, 200)
        self.assertIn("《沙盘看板》V1", json.loads(data)["text"])

    def test_resume_rejects_unknown_or_bad_run(self):
        status, _, _ = self._request("POST", "/api/resume", {"run_id": "20990101-000000"})
        self.assertEqual(status, 400)
        status, _, _ = self._request("POST", "/api/resume", {"run_id": "../x"})
        self.assertEqual(status, 400)

    def test_export_endpoint(self):
        self._fake_run()
        status, data, _ = self._request("GET", "/api/runs/20200101-000001/export")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["id"], "20200101-000001")
        self.assertEqual([b["version"] for b in payload["boards"]], [1, 2])

    def test_reviews_endpoint_and_mark(self):
        base = self._fake_run()
        (base / "复查清单.json").write_text(json.dumps({
            "due_at": "2020-01-01", "verdict": "执行主方案", "reviewed_at": ""},
            ensure_ascii=False), encoding="utf-8")
        status, data, _ = self._request("GET", "/api/reviews")
        self.assertEqual(status, 200)
        reviews = json.loads(data)["reviews"]
        self.assertEqual(reviews[0]["id"], "20200101-000001")
        self.assertTrue(reviews[0]["overdue"])

        status, data, _ = self._request("POST", "/api/runs/20200101-000001/review", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["review"]["reviewed_at"])

        status, data, _ = self._request("GET", "/api/reviews")
        self.assertFalse(json.loads(data)["reviews"][0]["overdue"])

    # ------------------------------------------------------------ 流式
    def test_stream_uses_chunked_and_frames_are_intact(self):
        self.session.bus.emit("info", text="阶段 1/6 ｜ 测试")
        raw = self._raw_stream(since=0)
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"HTTP/1.1 200", head)
        self.assertIn(b"Transfer-Encoding: chunked", head)
        self.assertIn(b"Content-Type: text/event-stream", head)
        match = re.match(rb"^([0-9A-F]+)\r\n", body)
        self.assertIsNotNone(match, body[:40])
        size = int(match.group(1), 16)
        payload = body[match.end():match.end() + size]
        self.assertTrue(payload.startswith(b"id: 1\n"), payload[:40])
        data_line = [line for line in payload.decode("utf-8").splitlines()
                     if line.startswith("data: ")]
        event = json.loads(data_line[0][len("data: "):])
        self.assertEqual(event["id"], 1)
        self.assertEqual(event["type"], "info")
        self.assertIn("阶段 1/6", event["text"])

    def test_stream_replays_from_since(self):
        for index in range(3):
            self.session.bus.emit("info", text="第 %d 条" % (index + 1))
        raw = self._raw_stream(since=2)
        body = raw.partition(b"\r\n\r\n")[2]
        ids = [json.loads(line[len("data: "):])["id"]
               for line in body.decode("utf-8").splitlines() if line.startswith("data: ")]
        self.assertEqual(ids, [3])

    def _answer_when_ready(self, text, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline and not self.session.bus.awaiting():
            if not self.session.snapshot()["active"]:
                break
            time.sleep(0.02)
        self.session.answer(text)

    # ------------------------------------------------------------ 接口边界
    def test_empty_answer_rejected(self):
        status, data, _ = self._request("POST", "/api/answer", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(data))

    def test_answer_when_idle_returns_409_without_user_event(self):
        status, data, _ = self._request("POST", "/api/answer", {"text": "空闲时作答"})
        self.assertEqual(status, 409)
        self.assertIn("已忽略", json.loads(data)["error"])
        users = [ev for ev in self.session.bus.read(0, timeout=0.01) if ev["type"] == "user"]
        self.assertEqual(users, [])

    def test_oversized_body_returns_readable_400(self):
        payload = b'{"query": "' + b"a" * (300 * 1024) + b'"}'
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/start", body=payload,
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": "application/json",
                              "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn("请求体超过 256 KB 上限", json.loads(resp.read())["error"])
        self.assertEqual(resp.getheader("Connection"), "close")
        conn.close()

    def test_bad_json_body_rejected(self):

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/answer", body=b"{not json",
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": "application/json",
                              "Content-Length": "9"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
        conn.close()

    def test_start_rejects_unknown_provider_over_http(self):
        status, _, _ = self._request("POST", "/api/start",
                                     {"query": "要不要买车", "provider": "evil"})
        self.assertEqual(status, 400)

    def test_stop_active_session(self):
        self.session.start("我要不要买一台二十万级的家用车", "mock")
        status, data, _ = self._request("POST", "/api/stop", {})
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertTrue(body["ok"])
        self.assertTrue(body["stopped"])
        deadline = time.time() + 15
        while time.time() < deadline and self.session.snapshot()["active"]:
            time.sleep(0.05)
        self.assertFalse(self.session.snapshot()["active"])
        status, data, _ = self._request("POST", "/api/stop", {})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(data)["stopped"])

    def test_null_origin_write_rejected(self):
        status, _, _ = self._request("POST", "/api/stop", {},
                                     headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_write_requires_json_content_type(self):
        status, _, _ = self._request("POST", "/api/stop", {},
                                     headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_write_rejects_none_fetch_site(self):
        status, _, _ = self._request("POST", "/api/reset", {},
                                     headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(status, 403)

    def test_busy_session_rejected_over_http(self):
        self.session.start("我要不要买一台二十万级的家用车", "mock")
        deadline = time.time() + 10
        while time.time() < deadline and not [e for e in self.session.bus.read(0, timeout=0.1)
                                              if e["type"] == "ask"]:
            pass
        status, data, _ = self._request("POST", "/api/reset", {})
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(data))
        status, _, _ = self._request("POST", "/api/start", {"query": "要不要再开一场"})
        self.assertEqual(status, 409)
        for answer in ("三个月内定下来", "月收入1.15万，存款120万", "继续打车或暂缓"):
            self._answer_when_ready(answer)
        self._answer_when_ready("结束沙盘，给我结论")
        deadline = time.time() + 15
        while time.time() < deadline and self.session.snapshot()["active"]:
            time.sleep(0.05)
        self.assertFalse(self.session.snapshot()["active"])


if __name__ == "__main__":
    unittest.main()