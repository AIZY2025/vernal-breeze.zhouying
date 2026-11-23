"""core/llm.py 分支补充测试：全部网络交互用 unittest.mock 打桩，不发真实请求。"""
import io
import json
import unittest
import urllib.error
from unittest.mock import call, patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.llm import LLMError, OpenAICompatible

URL = "https://api.test/v1/chat/completions"


class FakeResponse:
    """最小可用的 urlopen 返回对象（支持 with 语句与 read）。"""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BrokenBody(io.BytesIO):
    """读取响应体时直接失败，用于覆盖 detail 读取的 except 分支。"""

    def __init__(self):
        super().__init__(b"{}")

    def read(self, *args, **kwargs):
        raise OSError("body unreadable")


def http_error(code, body=b'{"error": "boom"}'):
    return urllib.error.HTTPError(URL, code, "boom", {}, io.BytesIO(body))


def ok_body(content):
    return FakeResponse(json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8"))


class InitTest(unittest.TestCase):
    def test_blank_base_url(self):
        with self.assertRaises(LLMError) as ctx:
            OpenAICompatible("", "k", "m")
        self.assertEqual(str(ctx.exception), "base_url 为空，无法调用模型")

    def test_blank_api_key(self):
        with self.assertRaises(LLMError) as ctx:
            OpenAICompatible("http://x", "", "m")
        self.assertEqual(str(ctx.exception), "api_key 为空，请填写 config.json 或环境变量")

    def test_blank_model(self):
        with self.assertRaises(LLMError) as ctx:
            OpenAICompatible("http://x", "k", "")
        self.assertEqual(str(ctx.exception), "model 为空，请填写模型 ID")

    def test_normal_attributes_and_trailing_slash(self):
        client = OpenAICompatible("https://api.test/v1///", "k", "m", timeout=12, max_tokens=7)
        self.assertEqual(client.base_url, "https://api.test/v1")
        self.assertEqual((client.api_key, client.model, client.timeout, client.max_tokens),
                         ("k", "m", 12, 7))

    def test_defaults(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        self.assertEqual(client.timeout, 90)
        self.assertEqual(client.max_tokens, 3000)


class ChatSuccessTest(unittest.TestCase):
    def test_request_payload_headers_and_return(self):
        client = OpenAICompatible("https://api.test/v1/", "k-1", "m-1", timeout=7, max_tokens=99)
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return ok_body("hello")

        with patch("core.llm.urllib.request.urlopen", side_effect=fake_urlopen) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                out = client.chat("SYS", [{"role": "user", "content": "hi"}], temperature=0.9)

        self.assertEqual(out, "hello")
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(sleep.called)
        self.assertEqual(captured["url"], URL)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(captured["headers"]["authorization"], "Bearer k-1")
        self.assertEqual(captured["headers"]["content-type"], "application/json")
        body = captured["body"]
        self.assertEqual(body["model"], "m-1")
        self.assertEqual(body["temperature"], 0.9)
        self.assertEqual(body["max_tokens"], 99)
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(body["messages"][1], {"role": "user", "content": "hi"})

    def test_segmented_content_is_joined(self):
        payload = {"choices": [{"message": {"content": [
            {"text": "你"}, "忽略非字典", {"text": "好"}, {"other": 1}]}}]}
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen",
                   return_value=FakeResponse(json.dumps(payload).encode("utf-8"))):
            self.assertEqual(client.chat("s", []), "你好")

    def test_non_ascii_is_encoded_once(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        bodies = []

        def fake_urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return ok_body("ok")

        with patch("core.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            client.chat("系统提示", [{"role": "user", "content": "中文问题"}])
        self.assertEqual(bodies[0]["messages"][1]["content"], "中文问题")


class ChatStructureErrorTest(unittest.TestCase):
    def _chat(self, payload):
        client = OpenAICompatible("https://api.test", "k", "m")
        raw = json.dumps(payload, ensure_ascii=False)
        with patch("core.llm.urllib.request.urlopen",
                   return_value=FakeResponse(raw.encode("utf-8"))) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        return str(ctx.exception), urlopen, sleep

    def test_empty_choices(self):
        message, urlopen, sleep = self._chat({"choices": []})
        self.assertTrue(message.startswith("模型返回结构异常："))
        self.assertIn('"choices": []', message)
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(sleep.called)

    def test_missing_choices_key(self):
        message, _, _ = self._chat({"id": "x"})
        self.assertIn("模型返回结构异常", message)

    def test_message_none(self):
        message, _, _ = self._chat({"choices": [{"message": None}]})
        self.assertEqual(message, "模型返回空内容")

    def test_empty_content(self):
        message, _, _ = self._chat({"choices": [{"message": {"content": ""}}]})
        self.assertEqual(message, "模型返回空内容")

    def test_empty_segmented_content(self):
        message, _, _ = self._chat({"choices": [{"message": {"content": []}}]})
        self.assertEqual(message, "模型返回空内容")


class ChatHttpErrorTest(unittest.TestCase):
    def test_400_fails_fast_without_retry(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen", side_effect=http_error(400)) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(sleep.called)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertNotIn("boom", str(ctx.exception))

    def test_401_fails_fast(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen", side_effect=http_error(401)) as urlopen:
            with patch("core.llm.time.sleep"):
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(urlopen.call_count, 1)
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_500_retries_then_succeeds(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        effects = [http_error(500), ok_body("done")]
        with patch("core.llm.urllib.request.urlopen", side_effect=effects) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                self.assertEqual(client.chat("s", []), "done")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(2)])

    def test_503_exhausts_three_attempts(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen",
                   side_effect=[http_error(503), http_error(503), http_error(503)]) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])
        self.assertIn("HTTP 503", str(ctx.exception))

    def test_retriable_codes_all_accepted(self):
        for code in (408, 429, 502, 504):
            with self.subTest(code=code):
                client = OpenAICompatible("https://api.test", "k", "m")
                with patch("core.llm.urllib.request.urlopen",
                           side_effect=[http_error(code), ok_body("ok")]):
                    with patch("core.llm.time.sleep") as sleep:
                        self.assertEqual(client.chat("s", []), "ok")
                self.assertEqual(sleep.call_args_list, [call(2)])

    def test_unreadable_error_body_is_tolerated(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        err = urllib.error.HTTPError(URL, 500, "x", {}, BrokenBody())
        with patch("core.llm.urllib.request.urlopen", side_effect=[err, err, err]):
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])
        self.assertEqual(str(ctx.exception), "HTTP 500")


class ChatNetworkErrorTest(unittest.TestCase):
    def test_url_error_retries_then_succeeds(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        effects = [urllib.error.URLError("down"), ok_body("back")]
        with patch("core.llm.urllib.request.urlopen", side_effect=effects) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                self.assertEqual(client.chat("s", []), "back")
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(2)])

    def test_url_error_gives_up(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        effects = [urllib.error.URLError("down")] * 3
        with patch("core.llm.urllib.request.urlopen", side_effect=effects) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])
        self.assertTrue(str(ctx.exception).startswith("网络错误："))

    def test_timeout_gives_up(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen",
                   side_effect=[TimeoutError("t")] * 3) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])
        self.assertIn("网络错误：", str(ctx.exception))

    def test_os_error_gives_up(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen",
                   side_effect=[OSError("socket")] * 3):
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertEqual(sleep.call_args_list, [call(2), call(4)])
        self.assertIn("socket", str(ctx.exception))


class ChatInvalidJsonTest(unittest.TestCase):
    def test_invalid_json_wrapped_as_llm_error_without_retry(self):
        client = OpenAICompatible("https://api.test", "k", "m")
        with patch("core.llm.urllib.request.urlopen",
                   return_value=FakeResponse(b"not-a-json")) as urlopen:
            with patch("core.llm.time.sleep") as sleep:
                with self.assertRaises(LLMError) as ctx:
                    client.chat("s", [])
        self.assertIn("模型返回不是合法 JSON", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(sleep.called)


if __name__ == "__main__":
    unittest.main()