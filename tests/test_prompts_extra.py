"""core/prompts.py 分支补充测试：提示词文件读取、标题剥离与缺失文件。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _support  # noqa: F401  （把项目根目录加入 sys.path）

import core.prompts as prompts
from core.prompts import FILES, NAMES, all_prompts, load_prompt


class LoadPromptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self._patch = patch("core.prompts.AGENT_DIR", self.dir)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_heading_and_blank_lines_are_stripped(self):
        target = self.dir / FILES["A1"]
        target.write_text("# 背景探询\n\n第一段\n\n第二段\n", encoding="utf-8")
        self.assertEqual(load_prompt("A1"), "第一段\n\n第二段")

    def test_multiple_leading_headings_are_stripped(self):
        target = self.dir / FILES["A2"]
        target.write_text("# 标题一\n# 标题二\n正文\n", encoding="utf-8")
        self.assertEqual(load_prompt("A2"), "正文")

    def test_inner_blank_line_is_preserved(self):
        target = self.dir / FILES["A3"]
        target.write_text("第一行\n\n第三行", encoding="utf-8")
        self.assertEqual(load_prompt("A3"), "第一行\n\n第三行")

    def test_missing_file_raises_with_full_path(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            load_prompt("A4")
        message = str(ctx.exception)
        self.assertIn("提示词文件缺失：", message)
        self.assertIn(FILES["A4"], message)

    def test_unknown_key_raises_key_error(self):
        with self.assertRaises(KeyError):
            load_prompt("A9")

    def test_all_prompts_returns_five_entries(self):
        for name in FILES.values():
            (self.dir / name).write_text("# 标题\n正文-%s\n" % name, encoding="utf-8")
        result = all_prompts()
        self.assertEqual(sorted(result), ["A1", "A2", "A3", "A4", "A5"])
        self.assertEqual(result["A1"], "正文-%s" % FILES["A1"])


class RealPromptFilesTest(unittest.TestCase):
    def test_agent_dir_points_to_agents_folder(self):
        self.assertEqual(prompts.AGENT_DIR.name, "agents")
        self.assertEqual(Path(prompts.AGENT_DIR).parent.name,
                         Path(prompts.__file__).resolve().parent.parent.name)

    def test_every_real_prompt_is_non_empty_without_markdown_heading(self):
        for key in FILES:
            with self.subTest(key=key):
                text = load_prompt(key)
                self.assertTrue(text.strip())
                self.assertFalse(text.startswith("# "))
                self.assertNotIn("# A1 背景探询 Agent", text)

    def test_a1_prompt_content_is_preserved(self):
        text = load_prompt("A1")
        self.assertIn("JSON", text)
        self.assertIn("背景探询", text)
        self.assertNotIn("A1 背景探询 Agent", text)

    def test_names_cover_all_agents(self):
        self.assertEqual(sorted(NAMES), sorted(FILES))
        self.assertEqual(NAMES["A1"], "背景探询 Agent")
        self.assertEqual(NAMES["A5"], "仲裁汇总 Agent")


if __name__ == "__main__":
    unittest.main()