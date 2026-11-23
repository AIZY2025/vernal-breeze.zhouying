"""外部情报层：检索词规划、条目清洗、元宝搜索调用与降级/缓存。"""
import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401

from core.search import (DEFAULT_ENDPOINT, SIGNED_ENDPOINT, IntelService, SearchError,
                         YuanbaoSearch, _enrich_items, _html_text, _near_duplicate,
                         archive_queries, archive_query, core_terms, extract_facts,
                         normalize_items, plan_queries, rank_items, relevance, render_intel,
                         sign_request)

PAGES = [
    {"title": "2026 年就业市场观察", "url": "https://example.com/a", "date": "2026-10-01",
     "site": "示例网", "score": 0.91, "passage": "<strong>行业</strong>招聘需求变化"},
    json.dumps({"title": "离职前要看的三个数据", "url": "https://example.com/b",
                "date": "2026-09-12", "site": "示例报", "score": 0.88,
                "content": "宏观与行业数据"}, ensure_ascii=False),
    {"title": "没有链接", "date": "2026-10-01", "score": 0.99},
    {"title": "没有日期", "url": "https://example.com/c", "score": 0.97},
    {"title": "VR 卡", "url": "https://example.com/vr", "date": "2026-10-01", "vr": True},
    {"title": "非 http 链接", "url": "ftp://example.com/x", "date": "2026-10-01"},
]


def _transport(pages):
    calls = []

    def transport(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return 200, json.dumps({"Response": {"Pages": pages, "Query": "q"}},
                               ensure_ascii=False)

    transport.calls = calls
    return transport


class PlanQueriesTest(unittest.TestCase):
    def test_two_queries_and_limit(self):
        queries = plan_queries("我要不要离职")
        self.assertEqual(len(queries), 2)
        self.assertIn("离职", queries[0])
        self.assertIn("趋势", queries[1])
        for text in queries:
            self.assertLessEqual(len(text), 60)

    def test_extra_is_appended(self):
        queries = plan_queries("要不要买车", "刚换了工作")
        self.assertIn("刚换了工作", queries[0])

    def test_empty_query(self):
        self.assertEqual(plan_queries("   "), [])

    def test_duplicate_trend_query_dropped(self):
        queries = plan_queries("离" * 60)
        self.assertEqual(len(queries), 1)


class PlanQueriesKindTest(unittest.TestCase):
    def test_kind_specific_suffix(self):
        queries = plan_queries("要不要换工作", kind="职业变动")
        self.assertEqual(len(queries), 2)
        self.assertIn("薪酬", queries[1])
        self.assertNotIn("数据 趋势 政策", queries[1])

    def test_unknown_kind_falls_back_to_default(self):
        self.assertIn("数据 趋势 政策", plan_queries("要不要买车", kind="未知")[1])


class CacheKeyTest(unittest.TestCase):
    def test_key_includes_page_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = IntelService(cache_dir=Path(tmp), max_items=5, per_site=2)
            second = IntelService(cache_dir=Path(tmp), max_items=8, per_site=2)
            self.assertNotEqual(first._cache_path(["q"]), second._cache_path(["q"]))


class ArchiveQueryTest(unittest.TestCase):
    def test_uses_city_money_and_window_instead_of_bare_question(self):
        archive = {
            "question": "明年底前深圳购300万自住房可行性",
            "window": "明年底之前",
            "finance": {"cash": 1000000, "max_one_time": 1000000,
                        "monthly_income": 11000},
            "facts": ["购房城市为深圳", "目前每月租金1500元", "家庭三人居住"],
            "needs": ["1. 长期自住"],
        }
        text = archive_query(archive)
        self.assertLessEqual(len(text), 60)
        self.assertIn("深圳", text)
        self.assertIn("100万", text)
        self.assertIn("明年底之前", text)

    def test_unclear_window_and_duplicate_money_are_skipped(self):
        archive = {"question": "要不要买车", "window": "未明确（用户未提供）",
                   "finance": {"cash": 200000, "max_one_time": 200000}}
        text = archive_query(archive)
        self.assertIn("买车", text)
        self.assertEqual(text.count("20万"), 1)
        self.assertNotIn("未明确", text)

    def test_extra_new_fact_is_appended(self):
        text = archive_query({"question": "要不要买车"}, "公司搬到南山")
        self.assertIn("公司搬到南山", text)

    def test_bad_archive_falls_back_to_extra(self):
        self.assertEqual(archive_query(None, "买车"), "买车")
        self.assertEqual(archive_query({}, ""), "")


class NormalizeItemsTest(unittest.TestCase):
    def test_only_items_with_url_and_date_survive(self):
        items = normalize_items(PAGES, "q")
        self.assertEqual(len(items), 2)
        urls = [item["url"] for item in items]
        self.assertEqual(urls, ["https://example.com/a", "https://example.com/b"])
        self.assertEqual(items[0]["excerpt"], "行业招聘需求变化")
        self.assertAlmostEqual(items[0]["score"], 0.91)

    def test_bad_page_shapes_ignored(self):
        self.assertEqual(normalize_items(["not json", 3, None, []], "q"), [])


class YuanbaoSearchTest(unittest.TestCase):
    def test_request_shape_and_parse(self):
        transport = _transport(PAGES)
        client = YuanbaoSearch("test-key", transport=transport)
        items = client.search("离职")
        self.assertEqual(len(items), 2)
        call = transport.calls[0]
        self.assertTrue(call["headers"]["Authorization"].startswith("Bearer test-key"))
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {"Query": "离职"})

    def test_error_object_raises(self):
        def transport(url, headers, body, timeout):
            return 200, json.dumps({"Error": {"Code": "UnauthorizedOperation",
                                              "Message": "bad key"}})

        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch("k", transport=transport).search("q")
        self.assertIn("UnauthorizedOperation", str(ctx.exception))

    def test_nested_response_error_raises(self):
        def transport(url, headers, body, timeout):
            return 200, json.dumps({"Response": {
                "Error": {"Code": "UnauthorizedOperation", "Message": "未授权操作。"}}})

        with self.assertRaises(SearchError) as ctx:
            YuanbaoSearch("k", transport=transport).search("q")
        self.assertIn("未授权操作", str(ctx.exception))

    def test_invalid_json_raises(self):
        with self.assertRaises(SearchError):
            YuanbaoSearch("k", transport=lambda *a: (200, "<html>")).search("q")

    def test_missing_response_raises(self):
        with self.assertRaises(SearchError):
            YuanbaoSearch("k", transport=lambda *a: (200, "{}")).search("q")

    def test_no_key_raises(self):
        with self.assertRaises(SearchError):
            YuanbaoSearch("").search("q")


class SignRequestTest(unittest.TestCase):
    def test_tc3_authorization_shape(self):
        headers = sign_request("AKIDtest", "secret", "wsa.tencentcloudapi.com",
                               b'{"Query":"x"}', timestamp=1551113065)
        auth = headers["Authorization"]
        self.assertTrue(auth.startswith(
            "TC3-HMAC-SHA256 Credential=AKIDtest/2019-02-25/wsa/tc3_request"))
        self.assertIn("SignedHeaders=content-type;host", auth)
        self.assertRegex(auth, r"Signature=[0-9a-f]{64}$")
        self.assertEqual(headers["X-TC-Action"], "SearchPro")
        self.assertEqual(headers["X-TC-Version"], "2025-05-08")
        self.assertEqual(headers["X-TC-Timestamp"], "1551113065")
        self.assertEqual(headers["Host"], "wsa.tencentcloudapi.com")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")

    def test_region_is_optional(self):
        self.assertNotIn("X-TC-Region",
                         sign_request("AKID1", "secret", "h", b"{}", timestamp=1))
        headers = sign_request("AKID1", "secret", "h", b"{}", timestamp=1,
                               region="ap-guangzhou")
        self.assertEqual(headers["X-TC-Region"], "ap-guangzhou")

    def test_signature_depends_on_payload_secret_and_time(self):
        base = sign_request("AKID1", "s", "h", b'{"Query":"a"}', timestamp=1)
        same = sign_request("AKID1", "s", "h", b'{"Query":"a"}', timestamp=1)
        changed_payload = sign_request("AKID1", "s", "h", b'{"Query":"b"}', timestamp=1)
        changed_secret = sign_request("AKID1", "other", "h", b'{"Query":"a"}', timestamp=1)
        changed_time = sign_request("AKID1", "s", "h", b'{"Query":"a"}', timestamp=2)
        self.assertEqual(base["Authorization"], same["Authorization"])
        for other in (changed_payload, changed_secret, changed_time):
            self.assertNotEqual(base["Authorization"], other["Authorization"])


class SignedSearchTest(unittest.TestCase):
    def test_secret_mode_uses_tc3_and_signed_endpoint(self):
        transport = _transport(PAGES)
        client = YuanbaoSearch(secret_id="AKIDtest", secret_key="secret",
                               transport=transport)
        self.assertTrue(client.signed)
        self.assertEqual(client.endpoint, SIGNED_ENDPOINT)
        self.assertEqual(len(client.search("离职")), 2)
        call = transport.calls[0]
        self.assertEqual(call["url"], SIGNED_ENDPOINT)
        self.assertTrue(call["headers"]["Authorization"].startswith("TC3-HMAC-SHA256 "))
        self.assertEqual(json.loads(call["body"].decode("utf-8")), {"Query": "离职"})

    def test_api_key_mode_keeps_bearer(self):
        transport = _transport(PAGES)
        client = YuanbaoSearch(api_key="key-1", transport=transport)
        self.assertFalse(client.signed)
        self.assertEqual(client.endpoint, DEFAULT_ENDPOINT)
        client.search("q")
        self.assertTrue(transport.calls[0]["headers"]["Authorization"].startswith("Bearer "))

    def test_has_credential(self):
        self.assertTrue(YuanbaoSearch(api_key="k").has_credential)
        self.assertTrue(YuanbaoSearch(secret_id="a", secret_key="b").has_credential)
        self.assertFalse(YuanbaoSearch(secret_id="a").has_credential)
        self.assertFalse(YuanbaoSearch().has_credential)


class IntelServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def test_without_key_is_skipped(self):
        intel = IntelService(api_key="", mode="auto", cache_dir=self.cache).collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("未配置搜索凭据", intel["reason"])
        self.assertEqual(intel["items"], [])

    def test_secret_credentials_activate(self):
        service = IntelService(secret_id="a", secret_key="b", mode="auto")
        self.assertTrue(service.active)
        self.assertTrue(service.client.signed)

    def test_mode_off(self):
        intel = IntelService(api_key="k", mode="off", cache_dir=self.cache).collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("关闭", intel["reason"])

    def test_cache_mode_miss_does_not_call_api(self):
        service = IntelService(api_key="k", mode="cache", cache_dir=self.cache)
        service.client.transport = lambda *a: (_ for _ in ()).throw(AssertionError("不应联网"))
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("缓存未命中", intel["reason"])

    def test_live_search_then_cache_hit(self):
        service = IntelService(api_key="k", mode="live", cache_dir=self.cache, max_items=5)
        transport = _transport(PAGES)
        service.client.transport = transport
        first = service.collect("要不要离职")
        self.assertEqual(first["status"], "已获取")
        self.assertEqual([item["id"] for item in first["items"]], [1, 2])
        self.assertEqual(len(transport.calls), 2)  # 两条检索词

        second = IntelService(api_key="k", mode="auto", cache_dir=self.cache).collect("要不要离职")
        self.assertEqual(second["status"], "缓存")
        self.assertEqual(len(second["items"]), 2)

    def test_search_failure_degrades_instead_of_raising(self):
        service = IntelService(api_key="k", mode="live", cache_dir=self.cache)

        def boom(*args):
            raise SearchError("网络错误：timed out")

        service.client.transport = boom
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertIn("timed out", intel["reason"])

    def test_empty_result_is_not_cached(self):
        service = IntelService(api_key="k", mode="live", cache_dir=self.cache)
        service.client.transport = lambda *a: (200, json.dumps({"Response": {"Pages": []}}))
        intel = service.collect("要不要离职")
        self.assertEqual(intel["status"], "未获取")
        self.assertEqual(list(self.cache.glob("*.json")), [])

    def test_active_flag(self):
        self.assertTrue(IntelService(api_key="k", mode="auto").active)
        self.assertFalse(IntelService(api_key="", mode="auto").active)
        self.assertTrue(IntelService(api_key="k", mode="cache").active)
        self.assertFalse(IntelService(api_key="k", mode="off").active)


class CoreTermsTest(unittest.TestCase):
    def test_spoken_words_are_removed(self):
        self.assertEqual(core_terms("我要不要现在换工作 怎么样"), ["换工作"])
        # 单字介词被拆掉后才能命中标题里的「深圳」「长沙」
        self.assertEqual(core_terms("从深圳回长沙发展 值得 纠结 吗"), ["深圳", "长沙发展", "值得"])

    def test_punctuation_and_duplicates_are_dropped(self):
        self.assertEqual(core_terms("深圳，深圳；回长沙！"), ["深圳", "长沙"])

    def test_fallback_keeps_whole_query_when_only_stopwords(self):
        self.assertEqual(core_terms("要不要"), ["要不要"])
        self.assertEqual(core_terms("是否，应该。"), ["是否 应该"])

    def test_empty_query_returns_empty_list(self):
        self.assertEqual(core_terms("，。！？"), [])
        self.assertEqual(core_terms(""), [])


class RelevanceTest(unittest.TestCase):
    def test_empty_terms_means_irrelevant(self):
        self.assertEqual(relevance({"title": "anything"}, []), 0.0)

    def test_title_hit_scores_higher_than_excerpt_hit(self):
        terms = ["深圳", "长沙"]
        title_hit = relevance({"title": "从深圳出发的通勤"}, terms)
        excerpt_hit = relevance({"title": "城市观察", "excerpt": "深圳通勤"}, terms)
        self.assertGreater(title_hit, excerpt_hit)
        self.assertEqual(title_hit, 0.5)
        self.assertEqual(excerpt_hit, 0.15)

    def test_score_is_clamped_to_one(self):
        self.assertLessEqual(relevance({"title": "深圳长沙", "excerpt": "深圳长沙"}, ["深圳", "长沙"]), 1.0)


class NearDuplicateTest(unittest.TestCase):
    def test_identical_title_after_normalization(self):
        self.assertTrue(_near_duplicate("深圳,湖南出现4大转变！", "深圳 湖南出现4大转变"))

    def test_high_bigram_overlap_is_duplicate(self):
        self.assertTrue(_near_duplicate("深圳湖南出现四大转变这些信号值得警惕",
                                        "深圳湖南出现四大转变这些信号值得关注"))

    def test_different_titles_are_kept(self):
        self.assertFalse(_near_duplicate("从深圳回长沙的就业观察", "2026 年行业薪资报告"))
        self.assertFalse(_near_duplicate("", "标题"))

    def test_rewritten_repost_with_shared_title_core_is_duplicate(self):
        # 实测元宝返回：同一篇文章被换掉前缀后重新发布
        self.assertTrue(_near_duplicate(
            "别再用老思路赚钱!深圳湖南出现4大转变,正在向更多城市扩散",
            "老百姓要提前做准备:深圳,湖南出现4大转变,正在蔓延,需深思"))


class RankItemsTest(unittest.TestCase):
    def _item(self, title, url, score=0.0, date="2026-11-01", site="", excerpt=""):
        return {"title": title, "url": url, "score": score, "date": date,
                "site": site, "excerpt": excerpt}

    def test_relevant_item_overtakes_equal_score(self):
        items = [self._item("行业薪资报告", "https://e.com/a", 0.8),
                 self._item("从深圳回长沙发展的真实经验", "https://e.com/b", 0.8)]
        ranked = rank_items(items, ["深圳", "长沙"], max_items=2)
        self.assertEqual(ranked[0]["url"], "https://e.com/b")

    def test_near_duplicate_reposts_are_merged(self):
        items = [self._item("深圳,湖南出现4大转变", "https://a.example/1", 0.9),
                 self._item("深圳 湖南出现4大转变！", "https://b.example/1", 0.88),
                 self._item("长沙落户政策变化", "https://c.example/1", 0.5)]
        ranked = rank_items(items, ["深圳", "长沙"], max_items=5)
        self.assertEqual([item["url"] for item in ranked],
                         ["https://a.example/1", "https://c.example/1"])

    def test_per_site_cap_prefers_higher_rank(self):
        items = [self._item("甲一", "https://a.example/1", 0.9),
                 self._item("甲二", "https://a.example/2", 0.8),
                 self._item("甲三", "https://a.example/3", 0.7),
                 self._item("乙一", "https://b.example/1", 0.1)]
        ranked = rank_items(items, ["甲"], max_items=5, per_site=2)
        self.assertEqual([item["url"] for item in ranked],
                         ["https://a.example/1", "https://a.example/2",
                          "https://b.example/1"])

    def test_url_dedupe_and_original_score_untouched(self):
        items = [self._item("标题一", "https://e.com/dup", 0.3),
                 self._item("标题二", "https://e.com/dup", 0.99)]
        ranked = rank_items(items, ["标题"])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["score"], 0.99)
        self.assertEqual(ranked[0]["rank_score"], round(0.55 * 0.99 + 0.35 + 0.10, 4))
        self.assertEqual(ranked[0]["relevance"], 1.0)

    def test_query_weight_demotes_trend_results(self):
        items = [self._item("主体情报", "https://e.com/base", 0.9),
                 self._item("趋势情报", "https://e.com/trend", 0.9)]
        items[0]["query_weight"] = 1.0
        items[1]["query_weight"] = 0.85
        ranked = rank_items(items, [])
        self.assertEqual([item["url"] for item in ranked],
                         ["https://e.com/base", "https://e.com/trend"])

    def test_recency_prefers_newer_date(self):
        fresh = self._item("同分情报", "https://e.com/fresh", 0.5, date="2026-11-20")
        stale = self._item("同分情报", "https://e.com/stale", 0.5, date="2019-01-01")
        stale["title"] = "同分情报（旧）"
        ranked = rank_items([stale, fresh], [], max_items=2)
        self.assertEqual(ranked[0]["url"], "https://e.com/fresh")

    def test_max_items_truncates(self):
        items = [self._item("标题%d" % idx, "https://e%d.com/" % idx, 0.5)
                 for idx in range(1, 5)]
        self.assertEqual(len(rank_items(items, [], max_items=2)), 2)


class RenderIntelTest(unittest.TestCase):
    def test_with_items(self):
        intel = {"provider": "元宝搜索", "retrieved_at": "2026-11-21 10:00:00",
                 "items": [{"id": 1, "title": "标题", "url": "https://e.com/1",
                            "date": "2026-10-01", "site": "站点", "excerpt": "摘要"}]}
        text = render_intel(intel)
        self.assertIn("[外1]", text)
        self.assertIn("https://e.com/1", text)

    def test_without_items(self):
        self.assertIn("未获取", render_intel({"reason": "未配置搜索密钥"}))


class ArchiveQueriesTest(unittest.TestCase):
    def test_region_budget_and_short_queries(self):
        archive = {
            "question": "我要不要买一台二十万级的家用车",
            "type": "消费支出",
            "window": "三个月内决定，最晚春节前提车",
            "finance": {"max_one_time": 200000, "cash": 1200000},
            "facts": ["我在深圳工作，每天通勤 30 公里"],
            "needs": ["降低通勤时间"],
        }
        queries = archive_queries(archive)
        self.assertGreaterEqual(len(queries), 3)
        self.assertTrue(all(len(text) <= 42 for text in queries))
        self.assertTrue(any("深圳" in text for text in queries))
        self.assertTrue(any("预算20万" in text for text in queries))
        self.assertTrue(any("政策" in text for text in queries))

    def test_duplicate_queries_are_removed(self):
        archive = {"question": "要不要考研", "finance": {}, "facts": []}
        queries = archive_queries(archive)
        self.assertEqual(len(queries), len(set(queries)))


class ExtractFactsTest(unittest.TestCase):
    def test_amounts_rates_keywords_and_dates(self):
        text = "2026年新能源补贴 1.2 万元，首付 6 万，贷款利率 3.5%，政策截止 2026-12-31"
        facts = extract_facts(text)
        self.assertTrue(any(entry["value"] == 12000 for entry in facts["amounts"]))
        self.assertTrue(any(entry["value"] == 3.5 for entry in facts["rates"]))
        self.assertIn("补贴", facts["keywords"])
        self.assertTrue(facts["dates"])

    def test_empty_text_returns_empty_groups(self):
        facts = extract_facts("")
        self.assertEqual(facts["amounts"], [])
        self.assertEqual(facts["keywords"], [])


class EnrichItemsTest(unittest.TestCase):
    def test_page_text_is_extracted_and_failures_degrade(self):
        markup = ("<html><body><h1>正文</h1><p>月供 1650 元，补贴 1.2 万元</p>"
                  "<script>忽略</script></body></html>")
        items = [{"id": 1, "url": "https://e.com/1", "title": "标题", "excerpt": "旧摘要"},
                 {"id": 2, "url": "https://e.com/2", "title": "失败", "excerpt": "旧摘要"}]

        def fetcher(url, timeout, max_bytes):
            if url.endswith("/2"):
                raise SearchError("网络错误")
            return markup

        result = _enrich_items(items, fetcher=fetcher)
        self.assertTrue(result[0]["page_status"].startswith("已抓取"))
        self.assertIn("1650", result[0]["content"])
        self.assertNotIn("忽略", result[0]["content"])
        self.assertTrue(result[0]["facts"]["amounts"])
        self.assertIn("未抓取", result[1]["page_status"])
        self.assertEqual(result[1]["excerpt"], "旧摘要")
        self.assertEqual(_html_text(markup), "月供 1650 元，补贴 1.2 万元")


if __name__ == "__main__":
    unittest.main()