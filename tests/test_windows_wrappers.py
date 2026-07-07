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

    def test_start_server_waits_for_existing_starting_process(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("function Get-LocalOcrProcess", script)
        self.assertIn("Get-Process -Id $serverPid", script)
        self.assertIn("API startup already in progress", script)
        self.assertIn("startup timed out while existing process is still running", script)

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

    def test_ocr_smart_wrapper_exists(self) -> None:
        self.assertTrue((ROOT / "ocr_smart.ps1").exists())

    def test_ocr_smart_routes_auto_pdf_to_ocr_for_codex(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("Resolve-SmartEngine", script)
        self.assertIn('".pdf"', script)
        self.assertIn('$RequestedEngine -ne "auto"', script)
        self.assertIn("simple_pdf_prefers_ocr", script)

    def test_ocr_smart_has_outer_timeout_and_compact_timeout_json(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$OuterTimeoutSec = 120", script)
        self.assertIn("WaitForExit($TimeoutSec * 1000)", script)
        self.assertIn("-TimeoutSec $OuterTimeoutSec", script)
        self.assertIn("client_timeout", script)
        self.assertIn("do_not_blindly_retry", script)

    def test_ocr_smart_checks_active_vl_before_work(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("Get-LocalOcrActiveTasks", script)
        self.assertIn("[l]ocalocr[.]cli", script)
        self.assertIn("[v]l_subprocess", script)
        self.assertIn("active_vl_task", script)

    def test_ocr_smart_active_task_probe_avoids_matching_itself(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("[l]ocalocr[.]cli|[v]l_subprocess", script)

    def test_api_wrappers_accept_model_profile_override(self) -> None:
        once = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")
        smart = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$Model", once)
        self.assertIn("body.model", once)
        self.assertIn("[string]$Model", smart)
        self.assertIn("-Model", smart)
        self.assertIn("requested_model", smart)


if __name__ == "__main__":
    unittest.main()
