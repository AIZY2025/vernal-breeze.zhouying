import tempfile
import unittest
from pathlib import Path

import _support

import run as entry


class PasteGuardTest(unittest.TestCase):
    def test_accepts_project_file(self):
        target = entry._safe_paste(entry.ROOT / "sample_archive.json")
        self.assertTrue(target.is_file())

    def test_rejects_outside_project(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8")
        handle.write("{}")
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        with self.assertRaises(SystemExit) as ctx:
            entry._safe_paste(handle.name)
        self.assertIn("必须位于项目目录内", str(ctx.exception))

    def test_rejects_directory(self):
        with self.assertRaises(SystemExit):
            entry._safe_paste(entry.ROOT)


if __name__ == "__main__":
    unittest.main()