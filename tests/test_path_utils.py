from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localocr.path_utils import to_windows_hint, to_wsl_path


class PathUtilsTest(unittest.TestCase):
    def test_windows_backslash_path_to_wsl(self):
        self.assertEqual(
            str(to_wsl_path(r"E:\LocalOCR\tests\samples\sample.png")),
            "/mnt/e/LocalOCR/tests/samples/sample.png",
        )

    def test_windows_slash_path_to_wsl(self):
        self.assertEqual(
            str(to_wsl_path("C:/Users/example/file.pdf")),
            "/mnt/c/Users/example/file.pdf",
        )

    def test_wsl_path_stays_wsl(self):
        self.assertEqual(
            str(to_wsl_path("/mnt/e/LocalOCR/tests/samples/sample.png")),
            "/mnt/e/LocalOCR/tests/samples/sample.png",
        )

    def test_windows_hint(self):
        self.assertEqual(
            to_windows_hint("/mnt/e/LocalOCR/outputs/a.md"),
            r"E:\LocalOCR\outputs\a.md",
        )


if __name__ == "__main__":
    unittest.main()
