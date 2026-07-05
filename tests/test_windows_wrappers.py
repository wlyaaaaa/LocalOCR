from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class WindowsWrapperTest(unittest.TestCase):
    def test_start_server_uses_named_mutex(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("System.Threading.Mutex", script)
        self.assertIn("WaitOne", script)
        self.assertIn("ReleaseMutex", script)
        self.assertIn("finally", script)

    def test_stop_server_cleans_api_vl_children_and_pid_file(self) -> None:
        script = (ROOT / "stop_server.ps1").read_text(encoding="utf-8")

        self.assertIn("localocr.cli", script)
        self.assertIn("_pdf_pages/api/vl_subprocess", script)
        self.assertIn("wsl-server.pid", script)
        self.assertIn("Remove-Item", script)


if __name__ == "__main__":
    unittest.main()
