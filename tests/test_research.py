"""检索请求：校验、去重、预算与证据池编号。"""
import unittest

import _support  # noqa: F401

from core import research


class NormalizeTest(unittest.TestCase):
    def test_invalid_and_oversized_requests_are_dropped(self):
        requests = research.normalize("A1", [
            {"query": ""},
            {"query": "短"},
            {"query": " 深圳 2026 新能源 补贴 政策 ", "purpose": "补全地域政策"},
            {"query": "第二条有效查询"},
            {"query": "第三条不应保留"},
        ])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["query"], "深圳 2026 新能源 补贴 政策")
        self.assertEqual(requests[0]["agent"], "A1")
        self.assertEqual(requests[0]["purpose"], "补全地域政策")


class BudgetTest(unittest.TestCase):
    def test_duplicate_and_agent_limit(self):
        state = research.new_state()
        first = {"agent": "A2", "query": "养车成本 2026"}
        ok, _ = research.check(state, first)
        self.assertTrue(ok)
        research.accept(state, first)

        ok, reason = research.check(state, first)
        self.assertFalse(ok)
        self.assertIn("重复", reason)

        ok, reason = research.check(state, {"agent": "A2", "query": "保险费用 2026"})
        self.assertFalse(ok)
        self.assertIn("上限", reason)

    def test_total_request_and_item_limits(self):
        state = research.new_state()
        state["used"] = research.MAX_REQUESTS
        ok, reason = research.check(state, {"agent": "A3", "query": "任意查询"})
        self.assertFalse(ok)
        self.assertIn("上限", reason)

        state = research.new_state()
        state["items"] = research.MAX_ITEMS
        ok, reason = research.check(state, {"agent": "A3", "query": "任意查询"})
        self.assertFalse(ok)
        self.assertIn("正文", reason)


class MergeTest(unittest.TestCase):
    def test_global_ids_and_origin(self):
        archive = {}
        first = {"agent": "A1", "query": "q1", "purpose": "p1"}
        second = {"agent": "A2", "query": "q2", "purpose": "p2"}
        intel = {"status": "已获取", "provider": "测试源", "retrieved_at": "t",
                 "items": [{"title": "a"}]}
        self.assertEqual(research.merge(archive, intel, first), 1)
        self.assertEqual(research.merge(archive, intel, second), 1)
        ext = archive["external"]
        self.assertEqual([item["id"] for item in ext["items"]], [1, 2])
        self.assertEqual(ext["items"][1]["origin"], "A2")
        self.assertEqual(ext["items"][1]["request"], "p2")
        self.assertEqual([row["hits"] for row in ext["requests"]], [1, 1])
        self.assertEqual(ext["queries"], ["q1", "q2"])

    def test_failure_keeps_existing_pool(self):
        archive = {"external": {"status": "已获取", "items": [{"id": 1}]}}
        hits = research.merge(
            archive, {"status": "未获取", "reason": "超时", "items": []},
            {"agent": "A2", "query": "q2", "purpose": ""})
        self.assertEqual(hits, 0)
        self.assertEqual(archive["external"]["status"], "已获取")
        self.assertEqual(archive["external"]["requests"][0]["reason"], "超时")


if __name__ == "__main__":
    unittest.main()