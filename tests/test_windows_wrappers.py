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

    def test_start_server_has_configurable_startup_timeout(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$StartupTimeoutSec = 600", script)
        self.assertIn("WaitOne([TimeSpan]::FromSeconds($StartupTimeoutSec))", script)
        self.assertIn("AddSeconds($StartupTimeoutSec)", script)

    def test_stop_server_cleans_api_vl_children_and_pid_file(self) -> None:
        script = (ROOT / "stop_server.ps1").read_text(encoding="utf-8")

        self.assertIn("localocr.cli", script)
        self.assertIn("_pdf_pages/api/vl_subprocess", script)
        self.assertIn("wsl-server.pid", script)
        self.assertIn("Remove-Item", script)
        self.assertIn("$WslTimeoutSec", script)
        self.assertIn("WaitForExit", script)

    def test_ocr_once_can_release_api_after_request(self) -> None:
        script = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$StopAfter", script)
        self.assertIn("stop_server.ps1", script)
        self.assertIn("finally", script)
        self.assertIn("Write-Warning", script)

    def test_ocr_once_passes_startup_timeout_to_server(self) -> None:
        script = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$StartupTimeoutSec = 600", script)
        self.assertIn("-StartupTimeoutSec $StartupTimeoutSec", script)

    def test_release_resources_wrapper_calls_stop_server(self) -> None:
        script = (ROOT / "release_resources.ps1").read_text(encoding="utf-8")

        self.assertIn("stop_server.ps1", script)
        self.assertIn("LocalOCR resources released", script)


if __name__ == "__main__":
    unittest.main()
