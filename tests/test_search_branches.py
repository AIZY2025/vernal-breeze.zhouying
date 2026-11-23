"""core/search.py（外部情报/联网检索）的分支补充测试。

覆盖范围：
1. plan_queries / normalize_items / render_intel 的纯函数分支；
2. sign_request 的 TC3-HMAC-SHA256 签名形状与确定性；
3. YuanbaoSearch 两种凭据模式、显式 endpoint、错误响应与 transport 注入；
4. IntelService 的 off/live/cache/auto 四种模式、缓存读写与过期、降级与截断去重；
5. _urllib_transport 的成功与三类异常（HTTPError / URLError / OSError）；
6. settings.build_research 的配置读取、默认值、非法值与凭据优先级。

所有网络请求均通过注入 fake transport 或 patch urlopen 拦截，不发起真实联网。
"""
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

from core.search import (DEFAULT_ENDPOINT, SIGNED_ENDPOINT, IntelService, SearchError,
                         YuanbaoSearch, _clean, _urllib_transport, normalize_items,
                         plan_queries, render_intel, sign_request)
from core.settings import build_research


# ---------------------------------------------------------------- 测试辅助
def page(**overrides):
    data = {"title": "标题", "url": "https://example.com/a", "date": "2026-10-01",
            "site": "示例网", "score": 0.9, "passage": "摘要正文"}
    data.update(overrides)
    return data


def transport_with(pages, status=200):
    """返回固定 Pages 的 fake transport，并记录每次调用。"""
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return status, json.dumps({"Response": {"Pages": pages}}, ensure_ascii=False)

    transport.calls = calls
    return transport


def transport_text(text, status=200):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return status, text

    transport.calls = calls
    return transport


def transport_by_query(handler):
    """按请求体里的 Query 分发，用于多检索词场景。"""
    def transport(url, headers, body, timeout):
        query = json.loads(body.decode("utf-8"))["Query"]
        return 200, json.dumps({"Response": {"Pages": handler(query)}}, ensure_ascii=False)

    return transport


class FakeResponse:
    """urlopen 的最小替身，支持 with 语句。"""

    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class BrokenBody(io.BytesIO):
    def __init__(self):
        super().__init__(b"ignored")

    def read(self, *args, **kwargs):
        raise OSError("body unreadable")


# ---------------------------------------------------------------- plan_queries
class PlanQueriesBranchTest(unittest.TestCase):
    def test_empty_and_blank_input_returns_no_query(self):
        for text in ("", "   ", None, "，。；！？"):
            with self.subTest(text=text):
                self.assertEqual(plan_queries(text), [])

    def test_extra_is_appended_after_punctuation_cleanup(self):
        queries = plan_queries("要不要买车", "刚换了工作，通勤变远")
        self.assertEqual(len(queries), 2)
        self.assertTrue(queries[0].startswith("要不要买车 刚换了工作 通勤变远"))
        self.assertNotIn("，", queries[0])

    def test_extra_with_only_punctuation_keeps_base(self):
        queries = plan_queries("要不要买车", "，。；")
        self.assertEqual(queries[0], "要不要买车")

    def test_query_is_truncated_to_sixty_chars(self):
        queries = plan_queries("车" * 100)
        self.assertEqual(len(queries[0]), 60)
        self.assertEqual(queries[0], "车" * 60)
        self.assertEqual(len(queries), 1)

    def test_trend_query_appends_keywords_within_limit(self):
        queries = plan_queries("要不要离职")
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[1], "要不要离职 最新 数据 趋势 政策")
        self.assertLessEqual(len(queries[1]), 60)

    def test_trend_equal_to_base_is_deduplicated(self):
        queries = plan_queries("离" * 60)
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0], queries[-1])

    def test_punctuation_and_spaces_are_normalized(self):
        queries = plan_queries("  要不要，买车。\n谢谢  ")
        self.assertEqual(queries[0], "要不要 买车 谢谢")


# ---------------------------------------------------------------- normalize_items
class NormalizeItemsBranchTest(unittest.TestCase):
    def test_complete_dict_item_is_mapped(self):
        items = normalize_items([page(score=0.91)], "要不要离职")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "标题")
        self.assertEqual(item["url"], "https://example.com/a")
        self.assertEqual(item["date"], "2026-10-01")
        self.assertEqual(item["site"], "示例网")
        self.assertAlmostEqual(item["score"], 0.91)
        self.assertEqual(item["excerpt"], "摘要正文")
        self.assertEqual(item["query"], "要不要离职")

    def test_json_string_item_is_parsed(self):
        raw = json.dumps(page(title="JSON 条目"), ensure_ascii=False)
        items = normalize_items([raw], "q")
        self.assertEqual([item["title"] for item in items], ["JSON 条目"])

    def test_non_json_string_is_dropped(self):
        self.assertEqual(normalize_items(["not-json"], "q"), [])

    def test_json_string_that_is_not_object_is_dropped(self):
        self.assertEqual(normalize_items(["[1, 2]", json.dumps([page()])], "q"), [])

    def test_non_dict_non_string_values_are_dropped(self):
        self.assertEqual(normalize_items([3, None, [], (), object()], "q"), [])

    def test_none_pages_is_treated_as_empty(self):
        self.assertEqual(normalize_items(None, "q"), [])

    def test_vr_item_is_dropped(self):
        self.assertEqual(normalize_items([page(vr=True)], "q"), [])

    def test_missing_url_is_dropped(self):
        raw = page()
        raw.pop("url")
        self.assertEqual(normalize_items([raw], "q"), [])

    def test_missing_date_is_dropped(self):
        raw = page()
        raw.pop("date")
        self.assertEqual(normalize_items([raw], "q"), [])

    def test_missing_title_is_dropped(self):
        self.assertEqual(normalize_items([page(title="")], "q"), [])

    def test_slash_date_from_live_search_is_accepted_and_normalized(self):
        # 元宝搜索真实返回 “2026/11/12 00:00:00”，不能被日期形状校验丢掉
        items = normalize_items([page(date="2026/11/12 00:00:00")], "q")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2026-11-12 00:00:00")

    def test_title_with_only_html_tags_becomes_empty_and_dropped(self):
        self.assertEqual(normalize_items([page(title="<b></b>")], "q"), [])

    def test_private_urls_are_dropped(self):
        for url in ("http://127.0.0.1/x", "http://localhost/x", "http://10.0.0.1/x",
                    "http://[::1]/x"):
            with self.subTest(url=url):
                self.assertEqual(normalize_items([page(url=url)], "q"), [])

    def test_non_http_urls_are_dropped(self):
        for url in ("ftp://example.com/x", "//example.com/x", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertEqual(normalize_items([page(url=url)], "q"), [])

    def test_https_url_is_accepted(self):
        items = normalize_items([page(url="https://example.com/b")], "q")
        self.assertEqual(items[0]["url"], "https://example.com/b")

    def test_non_numeric_score_falls_back_to_zero(self):
        for score in ("not-a-number", None, {"x": 1}, [1]):
            with self.subTest(score=score):
                items = normalize_items([page(score=score)], "q")
                self.assertEqual(items[0]["score"], 0.0)

    def test_numeric_string_score_is_cast(self):
        self.assertEqual(normalize_items([page(score="0.25")], "q")[0]["score"], 0.25)

    def test_passage_wins_over_content(self):
        items = normalize_items([page(passage="段落", content="正文")], "q")
        self.assertEqual(items[0]["excerpt"], "段落")

    def test_content_used_when_passage_missing_or_empty(self):
        missing = page(content="正文")
        missing.pop("passage")
        self.assertEqual(normalize_items([missing], "q")[0]["excerpt"], "正文")
        self.assertEqual(normalize_items([page(passage="", content="正文")], "q")[0]["excerpt"],
                         "正文")

    def test_excerpt_is_empty_when_both_missing(self):
        raw = page()
        raw.pop("passage")
        self.assertEqual(normalize_items([raw], "q")[0]["excerpt"], "")

    def test_html_tags_and_whitespace_are_cleaned(self):
        raw = page(title="<em>标题</em>\n\n  多行", passage="<p>第一段</p>\r\n第二段")
        item = normalize_items([raw], "q")[0]
        self.assertEqual(item["title"], "标题 多行")
        self.assertEqual(item["excerpt"], "第一段 第二段")

    def test_title_truncated_to_eighty(self):
        item = normalize_items([page(title="标" * 100)], "q")[0]
        self.assertEqual(len(item["title"]), 80)

    def test_site_truncated_to_twenty(self):
        item = normalize_items([page(site="站" * 30)], "q")[0]
        self.assertEqual(len(item["site"]), 20)

    def test_excerpt_truncated_to_two_hundred_twenty(self):
        item = normalize_items([page(passage="摘" * 300)], "q")[0]
        self.assertEqual(len(item["excerpt"]), 220)

    def test_date_truncated_to_nineteen(self):
        item = normalize_items([page(date="2026-10-01T10:00:00.123456Z")], "q")[0]
        self.assertEqual(item["date"], "2026-10-01T10:00:00")

    def test_clean_helper_handles_none(self):
        self.assertEqual(_clean(None, 10), "")


# ---------------------------------------------------------------- sign_request
class SignRequestBranchTest(unittest.TestCase):
    AUTH_1551113065 = ("TC3-HMAC-SHA256 Credential=AKIDtest/2019-02-25/wsa/tc3_request, "
                       "SignedHeaders=content-type;host, "
                       "Signature=75722a9876386a22246e9a54be9502876256257f6fed8417d88a50551791011b")

    def test_authorization_exact_shape_at_fixed_timestamp(self):
        headers = sign_request("AKIDtest", "secret", "wsa.tencentcloudapi.com",
                               b'{"Query":"x"}', timestamp=1551113065)
        self.assertEqual(headers["Authorization"], self.AUTH_1551113065)

    def test_utc_date_and_standard_headers(self):
        headers = sign_request("AKID1", "s", "wsa.tencentcloudapi.com", b"{}",
                               timestamp=1551113065)
        self.assertIn("/2019-02-25/wsa/tc3_request", headers["Authorization"])
        self.assertEqual(headers["Host"], "wsa.tencentcloudapi.com")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["X-TC-Action"], "SearchPro")
        self.assertEqual(headers["X-TC-Version"], "2025-05-08")
        self.assertEqual(headers["X-TC-Timestamp"], "1551113065")

    def test_utc_date_at_epoch(self):
        headers = sign_request("AKID1", "s", "h", b"{}", timestamp=0)
        self.assertIn("/1970-01-01/wsa/tc3_request", headers["Authorization"])

    def test_current_time_used_when_timestamp_is_none(self):
        with patch("core.search.time.time", return_value=1551113065):
            headers = sign_request("AKID1", "s", "h", b"{}")
        self.assertEqual(headers["X-TC-Timestamp"], "1551113065")
        self.assertIn("/2019-02-25/wsa/tc3_request", headers["Authorization"])

    def test_region_header_is_optional_and_not_signed(self):
        plain = sign_request("K", "s", "h", b"{}", timestamp=1)
        with_region = sign_request("K", "s", "h", b"{}", timestamp=1, region="ap-guangzhou")
        self.assertNotIn("X-TC-Region", plain)
        self.assertEqual(with_region["X-TC-Region"], "ap-guangzhou")
        self.assertEqual(plain["Authorization"], with_region["Authorization"])

    def test_signature_changes_with_payload(self):
        base = sign_request("K", "s", "h", b'{"Query":"a"}', timestamp=1)
        other = sign_request("K", "s", "h", b'{"Query":"b"}', timestamp=1)
        self.assertNotEqual(base["Authorization"], other["Authorization"])

    def test_signature_changes_with_secret(self):
        base = sign_request("K", "secret-a", "h", b"{}", timestamp=1)
        other = sign_request("K", "secret-b", "h", b"{}", timestamp=1)
        self.assertNotEqual(base["Authorization"], other["Authorization"])

    def test_signature_changes_with_timestamp(self):
        base = sign_request("K", "s", "h", b"{}", timestamp=1)
        other = sign_request("K", "s", "h", b"{}", timestamp=2)
        self.assertNotEqual(base["Authorization"], other["Authorization"])

    def test_same_inputs_are_deterministic(self):
        first = sign_request("K", "s", "h", b"{}", timestamp=123)
        second = sign_request("K", "s", "h", b"{}", timestamp=123)
        self.assertEqual(first, second)

    def test_str_and_bytes_payload_produce_same_signature(self):
        as_bytes = sign_request("K", "s", "h", '{"Query":"x"}'.encode("utf-8"), timestamp=1)
        as_str = sign_request("K", "s", "h", '{"Query":"x"}', timestamp=1)
        self.assertEqual(as_bytes["Authorization"], as_str["Authorization"])

    def test_custom_action_version_service_and_content_type(self):
        headers = sign_request("K", "s", "h", b"{}", timestamp=1551113065,
                               content_type="application/json",
                               action="OtherAction", version="2099-01-01", service="svc")
        self.assertIn("Credential=K/2019-02-25/svc/tc3_request", headers["Authorization"])
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-TC-Action"], "OtherAction")
        self.assertEqual(headers["X-TC-Version"], "2099-01-01")
        base = sign_request("K", "s", "h", b"{}", timestamp=1551113065)
        self.assertNotEqual(base["Authorization"], headers["Authorization"])


# ---------------------------------------------------------------- YuanbaoSearch
class YuanbaoSearchBranchTest(unittest.TestCase):
    def test_missing_credentials_raises_exact_message(self):
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch().search("q")
        self.assertEqual(str(ctx.exception),
                         "未配置搜索密钥（SecretId/SecretKey 或服务 API KEY）")

    def test_secret_mode_switches_endpoint_and_uses_tc3_headers(self):
        transport = transport_with([page()])
        client = YuanbaoSearch(secret_id="AKIDtest", secret_key="secret",
                               transport=transport, timeout=3.5)
        items = client.search("离职")
        self.assertTrue(client.signed)
        self.assertEqual(client.endpoint, SIGNED_ENDPOINT)
        self.assertEqual(len(items), 1)
        call = transport.calls[0]
        self.assertEqual(call["url"], SIGNED_ENDPOINT)
        self.assertEqual(call["timeout"], 3.5)
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {"Query": "离职"})
        headers = call["headers"]
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 "))
        self.assertEqual(headers["Host"], "wsa.tencentcloudapi.com")
        self.assertEqual(headers["X-TC-Action"], "SearchPro")
        self.assertEqual(headers["X-TC-Version"], "2025-05-08")
        self.assertIn("X-TC-Timestamp", headers)

    def test_secret_mode_passes_region_and_custom_version(self):
        transport = transport_with([page()])
        client = YuanbaoSearch(secret_id="K", secret_key="s", region="ap-shanghai",
                               version="2099-01-01", transport=transport)
        client.search("q")
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["X-TC-Region"], "ap-shanghai")
        self.assertEqual(headers["X-TC-Version"], "2099-01-01")

    def test_api_key_mode_keeps_bearer_and_default_endpoint(self):
        transport = transport_with([page()])
        client = YuanbaoSearch(api_key="test-key", transport=transport)
        client.search("q")
        self.assertFalse(client.signed)
        self.assertEqual(client.endpoint, DEFAULT_ENDPOINT)
        call = transport.calls[0]
        self.assertEqual(call["url"], DEFAULT_ENDPOINT)
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call["headers"]["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn("Host", call["headers"])

    def test_credentials_are_stripped(self):
        client = YuanbaoSearch(api_key="  key-padded  ",
                               secret_id="  AKID  ", secret_key="  sk  ")
        self.assertEqual(client.api_key, "key-padded")
        self.assertEqual((client.secret_id, client.secret_key), ("AKID", "sk"))
        self.assertTrue(client.signed)

    def test_explicit_endpoint_overrides_default_in_key_mode(self):
        transport = transport_with([page()])
        client = YuanbaoSearch(api_key="k", endpoint="https://custom.example/x", transport=transport)
        client.search("q")
        self.assertEqual(client.endpoint, "https://custom.example/x")
        self.assertEqual(transport.calls[0]["url"], "https://custom.example/x")

    def test_explicit_endpoint_overrides_default_in_secret_mode(self):
        transport = transport_with([page()])
        client = YuanbaoSearch(secret_id="K", secret_key="s",
                               endpoint="https://custom.example/x", transport=transport)
        client.search("q")
        self.assertEqual(client.endpoint, "https://custom.example/x")
        self.assertEqual(transport.calls[0]["url"], "https://custom.example/x")
        self.assertEqual(transport.calls[0]["headers"]["Host"], "custom.example")

    def test_non_https_endpoint_rejected(self):
        for endpoint in ("http://custom.example/x", "ftp://custom.example/x", "custom.example"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(SearchError):
                    YuanbaoSearch(api_key="k", endpoint=endpoint)

    def test_private_endpoint_rejected(self):
        for endpoint in ("https://127.0.0.1/x", "https://localhost/x", "https://10.0.0.1/x"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(SearchError):
                    YuanbaoSearch(api_key="k", endpoint=endpoint)

    def test_empty_version_falls_back_to_default(self):
        self.assertEqual(YuanbaoSearch(api_key="k", version="").version, "2025-05-08")

    def test_has_credential_accepts_either_mode(self):
        cases = [
            ({"api_key": "k"}, True),
            ({"secret_id": "a", "secret_key": "b"}, True),
            ({"secret_id": "a"}, False),
            ({"secret_key": "b"}, False),
            ({}, False),
            ({"api_key": "  "}, False),
        ]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(YuanbaoSearch(**kwargs).has_credential, expected)

    def test_top_level_error_raises_code_and_message(self):
        transport = transport_text(json.dumps(
            {"Error": {"Code": "AuthFailure", "Message": "签名错误"}}, ensure_ascii=False))
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch(api_key="k", transport=transport).search("q")
        self.assertEqual(str(ctx.exception), "AuthFailure：签名错误")

    def test_top_level_error_without_code_falls_through_to_missing_response(self):
        transport = transport_text(json.dumps({"Error": {"Message": "无 code"}}))
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch(api_key="k", transport=transport).search("q")
        self.assertEqual(str(ctx.exception), "搜索返回缺少 Response")

    def test_response_error_raises_code(self):
        transport = transport_text(json.dumps(
            {"Response": {"Error": {"Code": "UnauthorizedOperation", "Message": "未授权操作。"}}},
            ensure_ascii=False))
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch(api_key="k", transport=transport).search("q")
        self.assertEqual(str(ctx.exception), "UnauthorizedOperation：未授权操作。")

    def test_response_error_without_code_falls_through_to_pages(self):
        transport = transport_text(json.dumps(
            {"Response": {"Error": {"Message": "no code"}, "Pages": [page()]}}))
        items = YuanbaoSearch(api_key="k", transport=transport).search("q")
        self.assertEqual(len(items), 1)

    def test_invalid_json_raises_with_prefix(self):
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch(api_key="k", transport=transport_text("<html>oom</html>")).search("q")
        self.assertEqual(str(ctx.exception), "搜索返回不是合法 JSON：<html>oom</html>")

    def test_invalid_json_message_is_truncated_to_120_chars(self):
        text = "x" * 300
        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch(api_key="k", transport=transport_text(text)).search("q")
        self.assertEqual(str(ctx.exception), "搜索返回不是合法 JSON：" + "x" * 120)

    def test_non_dict_json_raises_structure_error(self):
        for text in ("[]", "[1, 2]", "123", '"text"'):
            with self.subTest(text=text):
                with self.assertRaises(SearchError) as ctx:
                    YuanbaoSearch(api_key="k", transport=transport_text(text)).search("q")
                self.assertEqual(str(ctx.exception), "搜索返回结构异常")

    def test_missing_response_raises(self):
        for payload in ({}, {"Response": None}, {"Response": [1]}):
            with self.subTest(payload=payload):
                transport = transport_text(json.dumps(payload))
                with self.assertRaises(SearchError) as ctx:
                    YuanbaoSearch(api_key="k", transport=transport).search("q")
                self.assertEqual(str(ctx.exception), "搜索返回缺少 Response")

    def test_empty_or_missing_pages_returns_empty_list(self):
        for payload in ({"Response": {}}, {"Response": {"Pages": []}},
                        {"Response": {"Pages": None}}):
            with self.subTest(payload=payload):
                transport = transport_text(json.dumps(payload))
                self.assertEqual(YuanbaoSearch(api_key="k", transport=transport).search("q"), [])

    def test_non_200_status_is_rejected(self):
        transport = transport_with([page()], status=500)
        with self.assertRaises(SearchError):
            YuanbaoSearch(api_key="k", transport=transport).search("q")

    def test_query_is_encoded_as_json_body(self):
        transport = transport_with([])
        YuanbaoSearch(api_key="k", transport=transport).search("养车 成本")
        self.assertEqual(json.loads(transport.calls[0]["body"].decode("utf-8")),
                         {"Query": "养车 成本"})


# ---------------------------------------------------------------- _urllib_transport
class UrllibTransportTest(unittest.TestCase):
    def setUp(self):
        self.url = "https://api.example/search"
        self.headers = {"Authorization": "Bearer k"}
        self.body = b'{"Query":"q"}'

    def test_success_returns_status_and_decoded_text(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
            captured["timeout"] = timeout
            return FakeResponse(200, "结果".encode("utf-8"))

        with patch("core.search.urllib.request.urlopen", side_effect=fake_urlopen):
            status, text = _urllib_transport(self.url, self.headers, self.body, 4.0)

        self.assertEqual((status, text), (200, "结果"))
        self.assertEqual(captured["url"], self.url)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["data"], self.body)
        self.assertEqual(captured["timeout"], 4.0)
        self.assertEqual(captured["headers"]["authorization"], "Bearer k")

    def test_http_error_hides_body_detail(self):
        error = urllib.error.HTTPError(self.url, 503, "boom", {},
                                       io.BytesIO("服务不可用".encode("utf-8")))
        with patch("core.search.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertEqual(str(ctx.exception), "HTTP 503")

    def test_http_error_body_is_not_echoed(self):
        error = urllib.error.HTTPError(self.url, 400, "bad", {},
                                       io.BytesIO(("d" * 500).encode("utf-8")))
        with patch("core.search.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertEqual(str(ctx.exception), "HTTP 400")

    def test_unreadable_http_error_body_is_tolerated(self):
        error = urllib.error.HTTPError(self.url, 500, "boom", {}, BrokenBody())
        with patch("core.search.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertEqual(str(ctx.exception), "HTTP 500")

    def test_urlerror_becomes_network_error(self):
        with patch("core.search.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("dns failed")):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertIn("网络错误：", str(ctx.exception))
        self.assertIn("dns failed", str(ctx.exception))

    def test_timeout_becomes_network_error(self):
        with patch("core.search.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertIn("网络错误：timed out", str(ctx.exception))

    def test_os_error_becomes_network_error(self):
        with patch("core.search.urllib.request.urlopen", side_effect=OSError("reset")):
            with self.assertRaises(SearchError) as ctx:
                _urllib_transport(self.url, self.headers, self.body, 4.0)
        self.assertIn("网络错误：reset", str(ctx.exception))


# ---------------------------------------------------------------- IntelService
class IntelServiceBranchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)

    def _service(self, **kwargs):
        kwargs.setdefault("api_key", "k")
        kwargs.setdefault("cache_dir", self.cache)
        return IntelService(**kwargs)

    def _write_cache(self, service, query, intel, saved_at=None):
        path = service._cache_path(plan_queries(query))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time.time() if saved_at is None else saved_at,
                                    "intel": intel}, ensure_ascii=False), encoding="utf-8")
        return path

    def _cached_intel(self, title="旧情报"):
        return {"status": "已获取", "provider": "元宝搜索", "rank": "v2",
                "retrieved_at": "2026-11-01 09:00:00",
                "queries": ["旧"], "items": [{"id": 1, "title": title,
                                                "url": "https://old.example/a",
                                                "date": "2026-10-01"}], "reason": ""}

    def test_mode_off_short_circuits_even_with_credentials(self):
        service = self._service(mode="off")
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "情报层已关闭")
        self.assertEqual(intel["items"], [])
        self.assertEqual(intel["queries"], plan_queries("要不要离职"))

    def test_mode_off_wins_over_empty_query(self):
        intel = self._service(mode="off").collect("，，。")
        self.assertEqual(intel["reason"], "情报层已关闭")

    def test_empty_query_reports_no_topic(self):
        intel = self._service(mode="live").collect("，。；")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "没有可检索的主题")
        self.assertEqual(intel["queries"], [])

    def test_auto_without_credential_is_skipped(self):
        intel = IntelService(api_key="", mode="auto", cache_dir=self.cache).collect("要不要离职")
        self.assertEqual(intel["reason"], "未配置搜索凭据（SecretId/SecretKey 或服务 API KEY）")

    def test_live_without_credential_is_skipped_before_transport(self):
        service = self._service(mode="live", api_key="")
        service.client.transport = lambda *a: (_ for _ in ()).throw(AssertionError("不应联网"))
        intel = service.collect("要不要离职")
        self.assertEqual(intel["reason"], "未配置搜索凭据（SecretId/SecretKey 或服务 API KEY）")

    def test_cache_mode_without_credential_still_reads_cache(self):
        service = IntelService(api_key="", mode="cache", cache_dir=self.cache)
        self._write_cache(service, "要不要离职", self._cached_intel())
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "缓存")
        self.assertEqual(len(intel["items"]), 1)

    def test_cache_miss_does_not_call_network(self):
        service = self._service(mode="cache")
        service.client.transport = lambda *a: (_ for _ in ()).throw(AssertionError("不应联网"))
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_cache_hit_returns_copy_with_fresh_status_and_queries(self):
        service = self._service(mode="cache")
        path = self._write_cache(service, "要不要离职", self._cached_intel())
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "缓存")
        self.assertEqual(intel["queries"], plan_queries("要不要离职"))
        self.assertNotEqual(intel["queries"], ["旧"])
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["intel"]["status"], "已获取")
        self.assertEqual(on_disk["intel"]["queries"], ["旧"])

    def test_live_success_writes_cache_and_assigns_ids(self):
        service = self._service(mode="live")
        service.client.transport = transport_with([
            page(score=0.5, title="第一条情报"),
            page(url="https://e.com/b", title="第二条情报")])
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")
        self.assertTrue(intel["retrieved_at"])
        self.assertEqual([item["id"] for item in intel["items"]], [1, 2])
        cached = list(self.cache.glob("*.json"))
        self.assertEqual(len(cached), 1)
        payload = json.loads(cached[0].read_text(encoding="utf-8"))
        self.assertIsInstance(payload["saved_at"], float)
        self.assertEqual(payload["intel"]["status"], "已获取")

    def test_auto_second_call_hits_cache(self):
        live = self._service(mode="live")
        live.client.transport = transport_with([page()])
        self.assertEqual(live.collect("要不要离职")["status"], "已获取")

        auto = self._service(mode="auto")
        auto.client.transport = lambda *a: (_ for _ in ()).throw(AssertionError("不应联网"))
        second = auto.collect("要不要离职")
        self.assertEqual(second["status"], "缓存")
        self.assertEqual(len(second["items"]), 1)

    def test_live_ignores_existing_cache(self):
        service = self._service(mode="live")
        self._write_cache(service, "要不要离职", self._cached_intel("旧情报"))
        service.client.transport = transport_with([page(title="新情报")])
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")
        self.assertEqual([item["title"] for item in intel["items"]], ["新情报"])

    def test_auto_refreshes_theme_cache_after_refresh_hours(self):
        service = self._service(mode="auto", refresh_hours=6)
        self._write_cache(service, "要不要离职", self._cached_intel("旧情报"),
                          saved_at=time.time() - 10 * 3600)
        service.client.transport = transport_with([page(title="新情报")])
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")
        self.assertEqual([item["title"] for item in intel["items"]], ["新情报"])

    def test_search_failure_degrades_to_not_retrieved(self):
        service = self._service(mode="live")

        def boom(*args):
            raise SearchError("网络错误：timed out")

        service.client.transport = boom
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("timed out", intel["reason"])
        self.assertEqual(intel["items"], [])

    def test_partial_query_failure_still_returns_items(self):
        service = self._service(mode="live")
        calls = {"n": 0}

        def transport(url, headers, body, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SearchError("网络错误：first failed")
            return 200, json.dumps({"Response": {"Pages": [page()]}}, ensure_ascii=False)

        service.client.transport = transport
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")
        self.assertEqual(len(intel["items"]), 1)

    def test_empty_result_is_not_cached(self):
        service = self._service(mode="live")
        service.client.transport = transport_with([])
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "搜索没有返回可用条目（需同时有链接与日期）")
        self.assertEqual(list(self.cache.glob("*.json")), [])

    def test_errors_are_joined_and_truncated_in_reason(self):
        service = self._service(mode="live")

        def boom(*args):
            raise SearchError("错" * 100)

        service.client.transport = boom
        intel = service.collect("要不要离职")
        self.assertEqual(len(intel["reason"]), 120)
        self.assertIn("；", intel["reason"])

    def test_max_items_truncates_and_urls_are_deduplicated(self):
        service = self._service(mode="live", max_items=2)

        def handler(query):
            return [page(url="https://e.com/dup", title="重复链接低分", score=0.5),
                    page(url="https://e.com/unique", title="独立链接高分", score=0.9),
                    page(url="https://e.com/dup", title="重复链接高分", score=0.99)]

        service.client.transport = transport_by_query(handler)
        intel = service.collect("要不要离职")
        self.assertEqual(len(intel["items"]), 2)
        self.assertEqual([item["url"] for item in intel["items"]],
                         ["https://e.com/dup", "https://e.com/unique"])
        self.assertEqual(intel["items"][0]["score"], 0.99)
        self.assertEqual([item["id"] for item in intel["items"]], [1, 2])
        self.assertEqual(intel["rank"], "v2")

    def test_per_site_cap_limits_items_from_one_site(self):
        service = self._service(mode="live", max_items=5, per_site=2)

        def handler(query):
            return [page(url="https://a.example/1", title="站点甲一", site="站点甲", score=0.9),
                    page(url="https://a.example/2", title="站点甲二", site="站点甲", score=0.8),
                    page(url="https://a.example/3", title="站点甲三", site="站点甲", score=0.7),
                    page(url="https://b.example/1", title="站点乙一", site="站点乙", score=0.6)]

        service.client.transport = transport_by_query(handler)
        intel = service.collect("要不要离职")
        self.assertEqual([item["url"] for item in intel["items"]],
                         ["https://a.example/1", "https://a.example/2",
                          "https://b.example/1"])

    def test_trend_query_results_are_downweighted(self):
        service = self._service(mode="live", max_items=5)

        def handler(query):
            if "趋势" in query:
                return [page(url="https://e.com/trend", title="泛化趋势情报", score=0.9)]
            return [page(url="https://e.com/base", title="主体情报", score=0.9)]

        service.client.transport = transport_by_query(handler)
        intel = service.collect("要不要离职")
        by_url = {item["url"]: item for item in intel["items"]}
        self.assertEqual(by_url["https://e.com/base"]["query_weight"], 1.0)
        self.assertEqual(by_url["https://e.com/trend"]["query_weight"], 0.85)
        self.assertEqual([item["url"] for item in intel["items"]],
                         ["https://e.com/base", "https://e.com/trend"])

    def test_expired_cache_is_ignored(self):
        service = self._service(mode="cache", cache_hours=24)
        self._write_cache(service, "要不要离职", self._cached_intel(),
                          saved_at=time.time() - 25 * 3600)
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_zero_cache_hours_disables_expiry_check(self):
        service = self._service(mode="cache", cache_hours=0)
        self._write_cache(service, "要不要离职", self._cached_intel(), saved_at=0)
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "缓存")

    def test_corrupted_cache_file_is_ignored(self):
        service = self._service(mode="cache")
        path = service._cache_path(plan_queries("要不要离职"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        intel = service.collect("要不要离职")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_cache_with_non_dict_intel_is_ignored(self):
        service = self._service(mode="cache")
        path = service._cache_path(plan_queries("要不要离职"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time.time(), "intel": "not-a-dict"}),
                        encoding="utf-8")
        intel = service.collect("要不要离职")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_old_cache_without_rank_version_is_not_reused(self):
        service = self._service(mode="cache")
        legacy = self._cached_intel()
        legacy.pop("rank")
        self._write_cache(service, "要不要离职", legacy)
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_cache_with_unparsable_saved_at_is_ignored(self):
        service = self._service(mode="cache")
        path = service._cache_path(plan_queries("要不要离职"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": "yesterday", "intel": self._cached_intel()}),
                        encoding="utf-8")
        intel = service.collect("要不要离职")
        self.assertEqual(intel["reason"], "缓存未命中（当前为缓存模式）")

    def test_cache_dir_none_skips_read_and_write(self):
        service = IntelService(api_key="k", mode="live", cache_dir=None)
        service.client.transport = transport_with([page()])
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")
        self.assertIsNone(service._cache_path(plan_queries("要不要离职")))
        self.assertIsNone(service._read_cache(None))
        service._write_cache(None, intel)

    def test_write_cache_oserror_is_swallowed(self):
        service = self._service(mode="live")
        service.client.transport = transport_with([page()])
        with patch.object(Path, "write_text", side_effect=OSError("denied")):
            intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "已获取")

    def test_cache_path_is_stable_and_distinct(self):
        service = self._service(mode="live")
        first = service._cache_path(plan_queries("要不要离职"))
        same = service._cache_path(plan_queries("要不要离职"))
        other = service._cache_path(plan_queries("要不要买车"))
        self.assertEqual(first, same)
        self.assertNotEqual(first, other)
        self.assertEqual(first.suffix, ".json")
        self.assertEqual(len(first.stem), 16)

    def test_constructor_defaults_and_coercions(self):
        service = IntelService(mode="AUTO")
        self.assertEqual(service.mode, "auto")
        self.assertEqual(service.max_items, 5)
        self.assertEqual(service.cache_hours, 24.0)
        self.assertEqual(IntelService(mode="").mode, "auto")
        self.assertEqual(IntelService(max_items=0).max_items, 5)
        self.assertEqual(IntelService(max_items=-3).max_items, 1)
        self.assertEqual(IntelService(max_items="3").max_items, 3)
        self.assertEqual(IntelService(cache_hours=None).cache_hours, 24.0)
        self.assertEqual(IntelService(cache_hours="12").cache_hours, 12.0)
        self.assertEqual(IntelService().per_site, 2)
        self.assertEqual(IntelService(per_site=0).per_site, 1)
        self.assertEqual(IntelService(per_site=None).per_site, 2)
        self.assertEqual(IntelService(per_site="many").per_site, 2)
        self.assertEqual(IntelService(per_site="3").per_site, 3)

    def test_constructor_rejects_non_numeric_values(self):
        with self.assertRaises(ValueError):
            IntelService(max_items="many")
        with self.assertRaises(ValueError):
            IntelService(cache_hours="soon")

    def test_active_flag_matrix(self):
        self.assertTrue(IntelService(api_key="k", mode="auto").active)
        self.assertFalse(IntelService(api_key="", mode="auto").active)
        self.assertTrue(IntelService(api_key="k", mode="cache").active)
        self.assertTrue(IntelService(api_key="", mode="cache").active)
        self.assertFalse(IntelService(api_key="", mode="live").active)
        self.assertFalse(IntelService(api_key="k", mode="off").active)
        self.assertFalse(IntelService(api_key="k", mode="unknown").active)

    def test_unknown_mode_is_not_fetched(self):
        service = self._service(mode="unknown")
        transport = transport_with([page()])
        service.client.transport = transport
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("未知的情报模式", intel["reason"])
        self.assertEqual(transport.calls, [])


# ---------------------------------------------------------------- render_intel
class RenderIntelBranchTest(unittest.TestCase):
    def _intel(self, **overrides):
        data = {"provider": "元宝搜索", "retrieved_at": "2026-11-21 10:00:00",
                "items": [{"id": 1, "title": "标题", "url": "https://e.com/1",
                           "date": "2026-10-01", "site": "站点", "excerpt": "摘要"}]}
        data.update(overrides)
        return data

    def test_items_with_excerpt_and_detail(self):
        text = render_intel(self._intel())
        self.assertIn("外部情报：1 条 ｜ 元宝搜索 ｜ 抓取于 2026-11-21 10:00:00", text)
        self.assertIn("[外1] 标题", text)
        self.assertIn("      站点 ｜ 2026-10-01", text)
        self.assertIn("      摘要：摘要", text)
        self.assertIn("      https://e.com/1", text)

    def test_detail_false_hides_excerpt(self):
        text = render_intel(self._intel(), detail=False)
        self.assertNotIn("摘要：", text)
        self.assertIn("https://e.com/1", text)

    def test_item_without_excerpt_has_no_summary_line(self):
        item = {"id": 2, "title": "无摘要", "url": "https://e.com/2", "date": "2026-10-01"}
        text = render_intel(self._intel(items=[item]))
        self.assertNotIn("摘要：", text)
        self.assertIn("      来源 ｜ 2026-10-01", text)

    def test_missing_id_renders_dash(self):
        item = {"title": "无 id", "url": "https://e.com/3", "date": "2026-10-01"}
        text = render_intel(self._intel(items=[item]))
        self.assertIn("[外-] 无 id", text)

    def test_excerpt_is_truncated_to_120_chars_in_render(self):
        long_excerpt = "摘" * 200
        item = {"id": 1, "title": "t", "url": "https://e.com/1",
                "date": "2026-10-01", "excerpt": long_excerpt}
        text = render_intel(self._intel(items=[item]))
        self.assertIn("摘要：" + long_excerpt[:120], text)
        self.assertNotIn(long_excerpt[:121], text)

    def test_without_items_uses_reason(self):
        text = render_intel({"reason": "未配置搜索密钥"})
        self.assertEqual(text, "外部情报：未获取 ｜ 未配置搜索密钥")

    def test_empty_intel_uses_unknown_reason(self):
        self.assertEqual(render_intel({}), "外部情报：未获取 ｜ 未知原因")

    def test_none_intel_uses_unknown_reason(self):
        self.assertEqual(render_intel(None), "外部情报：未获取 ｜ 未知原因")


# ---------------------------------------------------------------- build_research
class BuildResearchBranchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = Path(self._tmp.name)

    def test_missing_or_non_dict_search_section_disables_layer(self):
        self.assertIsNone(build_research({}))
        self.assertIsNone(build_research({"search": []}))
        self.assertIsNone(build_research({"search": "on"}))

    def test_enabled_false_wins_over_invalid_mode(self):
        self.assertIsNone(build_research({"search": {"enabled": False, "mode": "fast"}}))

    def test_mode_off_disables_layer(self):
        self.assertIsNone(build_research({"search": {"enabled": True, "mode": "off"}}))

    def test_invalid_mode_raises_system_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"mode": "fast"}})
        self.assertIn("search.mode 只能是 auto / live / cache / off", str(ctx.exception))
        self.assertIn("'fast'", str(ctx.exception))

    def test_mode_is_lowercased(self):
        service = build_research({"search": {"mode": "LIVE", "api_key": "k"}},
                                 cache_dir=self.cache)
        self.assertEqual(service.mode, "live")

    def test_default_mode_is_auto(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k"}}, cache_dir=self.cache)
        self.assertEqual(service.mode, "auto")

    def test_credentials_from_config(self):
        service = build_research({"search": {"secret_id": "AKID1", "secret_key": "sk",
                                             "mode": "live"}}, cache_dir=self.cache)
        self.assertTrue(service.client.signed)
        self.assertEqual(service.client.secret_id, "AKID1")
        self.assertEqual(service.client.secret_key, "sk")
        self.assertEqual(service.client.endpoint, SIGNED_ENDPOINT)

    def test_secret_credentials_from_default_environment_names(self):
        env = {"TENCENTCLOUD_SECRET_ID": "AKIDenv", "TENCENTCLOUD_SECRET_KEY": "sk-env"}
        with patch.dict(os.environ, env, clear=True):
            service = build_research({"search": {"mode": "live"}}, cache_dir=self.cache)
        self.assertEqual(service.client.secret_id, "AKIDenv")
        self.assertEqual(service.client.secret_key, "sk-env")
        self.assertTrue(service.client.signed)

    def test_secret_key_from_custom_environment_name(self):
        env = {"TENCENTCLOUD_SECRET_ID": "AKIDenv", "MY_SK": "sk-custom"}
        with patch.dict(os.environ, env, clear=True):
            service = build_research({"search": {"secret_key_env": "MY_SK"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.secret_key, "sk-custom")

    def test_api_key_from_config(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "sk-config"}}, cache_dir=self.cache)
        self.assertEqual(service.client.api_key, "sk-config")
        self.assertEqual(service.client.endpoint, DEFAULT_ENDPOINT)

    def test_api_key_from_default_environment_name(self):
        with patch.dict(os.environ, {"TENCENTCLOUD_WSA_APIKEY": "sk-env"}, clear=True):
            service = build_research({"search": {}}, cache_dir=self.cache)
        self.assertEqual(service.client.api_key, "sk-env")

    def test_api_key_from_custom_environment_name(self):
        with patch.dict(os.environ, {"MY_WSA": "sk-custom"}, clear=True):
            service = build_research({"search": {"api_key_env": "MY_WSA"}}, cache_dir=self.cache)
        self.assertEqual(service.client.api_key, "sk-custom")

    def test_secret_credentials_take_precedence_over_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "sk-config",
                                                 "secret_id": "AKID", "secret_key": "sk"}},
                                     cache_dir=self.cache)
        self.assertTrue(service.client.signed)
        self.assertEqual(service.client.endpoint, SIGNED_ENDPOINT)

    def test_numeric_options_are_mapped(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k", "timeout": "5",
                                                 "max_items": "3", "cache_hours": "12"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.timeout, 5.0)
        self.assertEqual(service.max_items, 3)
        self.assertEqual(service.cache_hours, 12.0)
        self.assertEqual(service.cache_dir, self.cache)

    def test_missing_numeric_options_use_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k"}}, cache_dir=self.cache)
        self.assertEqual(service.client.timeout, 8.0)
        self.assertEqual(service.max_items, 5)
        self.assertEqual(service.cache_hours, 24.0)

    def test_blank_numeric_options_use_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k", "timeout": "",
                                                 "max_items": "", "cache_hours": ""}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.timeout, 8.0)
        self.assertEqual(service.max_items, 5)
        self.assertEqual(service.cache_hours, 24.0)

    def test_zero_values_are_respected(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k", "timeout": "0",
                                                 "max_items": "0", "cache_hours": "0"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.timeout, 0.0)
        self.assertEqual(service.max_items, 5)
        self.assertEqual(service.cache_hours, 0.0)

    def test_invalid_timeout_raises_system_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"timeout": "慢"}})
        self.assertIn("search.timeout 必须是数字", str(ctx.exception))

    def test_invalid_max_items_raises_system_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"max_items": "很多"}})
        self.assertIn("search.max_items 必须是整数", str(ctx.exception))

    def test_invalid_cache_hours_raises_system_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"cache_hours": "很久"}})
        self.assertIn("search.cache_hours 必须是数字", str(ctx.exception))

    def test_per_site_is_mapped_and_invalid_raises_system_exit(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k", "per_site": "1"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.per_site, 1)
        with self.assertRaises(SystemExit) as ctx:
            build_research({"search": {"per_site": "很多"}})
        self.assertIn("search.per_site 必须是整数", str(ctx.exception))

    def test_blank_endpoint_uses_mode_specific_default(self):
        with patch.dict(os.environ, {}, clear=True):
            key_mode = build_research({"search": {"api_key": "k", "endpoint": ""}},
                                      cache_dir=self.cache)
            secret_mode = build_research({"search": {"secret_id": "AKID", "secret_key": "sk",
                                                     "endpoint": ""}}, cache_dir=self.cache)
        self.assertEqual(key_mode.client.endpoint, DEFAULT_ENDPOINT)
        self.assertEqual(secret_mode.client.endpoint, SIGNED_ENDPOINT)

    def test_explicit_endpoint_is_used(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"api_key": "k",
                                                 "endpoint": "https://custom.example/s"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.endpoint, "https://custom.example/s")

    def test_region_is_mapped(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {"secret_id": "AKID", "secret_key": "sk",
                                                 "region": "ap-guangzhou"}},
                                     cache_dir=self.cache)
        self.assertEqual(service.client.region, "ap-guangzhou")

    def test_no_credentials_keeps_layer_inactive(self):
        with patch.dict(os.environ, {}, clear=True):
            service = build_research({"search": {}}, cache_dir=self.cache)
        self.assertFalse(service.active)
        self.assertFalse(service.client.has_credential)


if __name__ == "__main__":
    unittest.main()