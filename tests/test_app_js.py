import unittest

import _support

APP_JS = _support.ROOT / "web" / "static" / "app.js"


class FinalDetectionTest(unittest.TestCase):
    def setUp(self):
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_uses_structured_final_flag(self):
        self.assertIn("ev.final === true", self.source)

    def test_does_not_match_report_by_body_keyword(self):
        self.assertNotIn("indexOf('最终报告')", self.source)
        self.assertNotIn('indexOf("最终报告")', self.source)

    def test_keeps_closed_marker_fallback(self):
        self.assertIn("_SESSION=CLOSED", self.source)


class VerdictHighlightTest(unittest.TestCase):
    def setUp(self):
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_verdict_is_split_and_highlighted(self):
        self.assertIn("function splitVerdict", self.source)
        self.assertIn("【仲裁判定】", self.source)
        self.assertIn("verdict-label", self.source)
        self.assertIn("verdict-text", self.source)

    def test_download_keeps_full_text(self):
        self.assertIn("downloadButton(text", self.source)


class StopButtonTest(unittest.TestCase):
    def setUp(self):
        self.source = APP_JS.read_text(encoding="utf-8")
        self.html = (_support.ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    def test_stop_button_and_endpoint_wired(self):
        self.assertIn('id="stop-btn"', self.html)
        self.assertIn("'/api/stop'", self.source)

    def test_stopped_done_branch_is_rendered(self):
        self.assertIn("ev.stopped", self.source)
        self.assertIn("未生成最终报告", self.source)


class ResumeHistoryTest(unittest.TestCase):
    def setUp(self):
        self.source = APP_JS.read_text(encoding="utf-8")

    def test_resume_endpoint_and_files_api_wired(self):
        self.assertIn("'/api/resume'", self.source)
        self.assertIn("/files", self.source)
        self.assertIn("function openRun", self.source)
        self.assertIn("function loadRunFile", self.source)

    def test_resume_row_is_rendered(self):
        self.assertIn("继续这场推演", self.source)
        self.assertIn("run-row--resume", self.source)
        self.assertIn("item.resumable", self.source)

    def test_export_wired(self):
        self.assertIn("/export", self.source)
        self.assertIn("function exportRun", self.source)
        self.assertIn("downloadJson", self.source)

    def test_stats_event_is_displayed(self):
        self.assertIn("case 'stats'", self.source)
        self.assertIn("模型调用", self.source)

    def test_review_loop_wired(self):
        self.assertIn("/api/reviews", self.source)
        self.assertIn("function checkReviews", self.source)
        self.assertIn("标记已复查", self.source)


if __name__ == "__main__":
    unittest.main()