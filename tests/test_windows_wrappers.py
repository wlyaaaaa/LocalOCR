from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _decode_process_output(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="strict").lstrip("\ufeff")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="strict")
    for encoding in ("utf-8", "gb18030", "utf-16-le", "utf-16-be"):
        try:
            decoded = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
        if "\x00" not in decoded or encoding.startswith("utf-16"):
            return decoded
    # Preserve undecodable bytes in diagnostics instead of dropping them.
    return raw.decode("utf-8", errors="surrogateescape")


class WindowsWrapperTest(unittest.TestCase):
    def _windows_script_path(self, path: Path) -> str:
        if os.name == "nt":
            return str(path)
        return subprocess.run(
            ["wslpath", "-w", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_start_converts_chinese_windows_path_before_cli(self) -> None:
        executable = "pwsh" if os.name == "nt" else "pwsh.exe"
        self._assert_start_converts_chinese_windows_path_before_cli(executable)

    def test_drag_drop_host_converts_chinese_windows_path_before_cli(self) -> None:
        batch = (ROOT / "start.bat").read_text(encoding="utf-8")
        launch = next(
            line for line in batch.splitlines()
            if "-File" in line and "start.ps1" in line
        )
        executable = launch.split()[0].strip('"')
        if os.name != "nt" and not executable.lower().endswith(".exe"):
            executable += ".exe"
        self._assert_start_converts_chinese_windows_path_before_cli(executable)

    def test_drag_drop_batch_launches_selected_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            batch = folder / "start.bat"
            batch.write_bytes((ROOT / "start.bat").read_bytes())
            input_path = folder / "中文目录" / "示例 图片.png"
            input_path.parent.mkdir()
            input_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            capture = folder / "selected-input.json"
            input_windows = self._windows_script_path(input_path)
            capture_windows = self._windows_script_path(capture).replace("'", "''")
            # The real batch must select its adjacent script and preserve the input.
            # This harmless script stops the chain before WSL or model execution.
            (folder / "start.ps1").write_text(
                "param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Inputs)\n"
                f"[IO.File]::WriteAllText('{capture_windows}', "
                "(ConvertTo-Json -InputObject @($Inputs) -Compress), "
                "[Text.UTF8Encoding]::new($false))\n",
                encoding="utf-8-sig",
            )
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", self._windows_script_path(batch), input_windows],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, _decode_process_output(completed.stderr))
            self.assertTrue(capture.is_file(), _decode_process_output(completed.stdout))
            self.assertEqual(json.loads(capture.read_text(encoding="utf-8")), [input_windows])

    def _assert_start_converts_chinese_windows_path_before_cli(self, executable: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Run the exact release wrapper bytes from a real Windows temporary path,
            # even when its immutable source snapshot lives on the WSL filesystem.
            script_copy = Path(temp_dir) / "start.ps1"
            script_copy.write_bytes((ROOT / "start.ps1").read_bytes())
            script_path = self._windows_script_path(script_copy)
            input_path = Path(temp_dir) / "中文目录" / "示例 图片.png"
            input_path.parent.mkdir()
            input_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            capture_path = Path(temp_dir) / "wsl-arguments.json"
            input_windows = self._windows_script_path(input_path)
            capture_windows = self._windows_script_path(capture_path)
            normalized_input = input_windows.replace("\\", "/")
            self.assertRegex(normalized_input, r"^[A-Za-z]:/")
            expected_wsl_path = f"/mnt/{normalized_input[0].lower()}{normalized_input[2:]}"

            quoted_script = script_path.replace("'", "''")
            quoted_input = input_windows.replace("'", "''")
            quoted_capture = capture_windows.replace("'", "''")
            command = f"""
function wsl {{
    param([string]$d, [string]$e, [string]$c)
    [IO.File]::WriteAllText(
        '{quoted_capture}',
        (@('-d', $d, '-e', $e, '-c', $c) | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
    $global:LASTEXITCODE = 0
}}
& '{quoted_script}' '{quoted_input}'
"""
            completed = subprocess.run(
                [executable, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                check=False,
                capture_output=True,
                text=False,
                timeout=20,
            )

            stderr = _decode_process_output(completed.stderr)
            self.assertEqual(completed.returncode, 0, stderr)
            self.assertTrue(capture_path.is_file(), _decode_process_output(completed.stdout))
            wsl_arguments = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertIn(expected_wsl_path, wsl_arguments[-1])

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

    def test_start_server_rechecks_health_while_holding_startup_mutex(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("Get-LocalOcrHealth", script)
        self.assertIn("System.Threading.Mutex", script)
        self.assertIn("WaitOne", script)
        self.assertIn("StartupTimeoutSec", script)
        self.assertNotIn("Get-LocalOcrProcess", script)

    def test_start_server_fails_fast_on_non_localocr_health(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("Assert-LocalOcrHealthPayload", script)
        self.assertIn("service", script)
        self.assertIn("localocr", script)
        self.assertIn("legacy_unknown", script)
        self.assertIn("readiness_unknown", script)
        self.assertIn("non-LocalOCR service", script)

    def test_start_server_fails_fast_when_windows_cannot_bind_port(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("Assert-LocalOcrPortBindable", script)
        self.assertIn("TcpListener", script)
        self.assertIn("excluded port ranges", script)

    def test_start_server_uses_detached_ported_launcher(self) -> None:
        script = (ROOT / "start_server.ps1").read_text(encoding="utf-8")

        self.assertIn("Start-LocalOcrServerProcess", script)
        self.assertIn("run_in_wsl.sh", script)
        self.assertIn("--host", script)
        self.assertIn("--port", script)
        self.assertIn("wsl-launcher.log", script)
        self.assertNotIn("Start-Process", script)

    def test_stop_server_uses_verified_pid_start_identity_and_target_port(self) -> None:
        script = (ROOT / "stop_server.ps1").read_text(encoding="utf-8")

        self.assertIn("server_pid", script)
        self.assertIn("server_start_time", script)
        self.assertIn("Get-ServerByPid", script)
        self.assertIn("Assert-TargetOwnsPort", script)
        self.assertIn("Test-WindowsPortOccupied", script)
        self.assertIn("scripts/run_in_wsl.sh", script)
        self.assertNotIn("/root/localocr-venv/bin/python", script)
        self.assertIn("psutil", script)
        self.assertIn("GraceSec", script)
        self.assertIn('"TERM"', script)
        self.assertIn('"KILL"', script)
        self.assertNotIn("pgrep", script)
        self.assertNotIn("pkill", script)
        self.assertIn("wsl-server.pid", script)
        self.assertIn("Remove-Item", script)
        self.assertIn("$WslTimeoutSec", script)
        self.assertIn("WaitForExit", script)
        self.assertIn('throw "[LocalOCR] Stop failed:', script)

    def test_ocr_once_can_release_api_after_request(self) -> None:
        script = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")

        self.assertIn("[switch]$StopAfter", script)
        self.assertIn("stop_server.ps1", script)
        self.assertIn("finally", script)
        self.assertIn("cleanup_failed", script)
        self.assertIn("resource_release_failed", script)
        self.assertIn("ocr_result", script)

    def test_ocr_once_passes_startup_timeout_to_server(self) -> None:
        script = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$StartupTimeoutSec = 600", script)
        self.assertIn("[int]$ExecutionTimeoutSec = 300", script)
        self.assertIn("timeout_sec = $ExecutionTimeoutSec", script)
        self.assertIn("-StartupTimeoutSec $StartupTimeoutSec", script)

    def test_release_resources_wrapper_calls_stop_server(self) -> None:
        script = (ROOT / "release_resources.ps1").read_text(encoding="utf-8")

        self.assertIn("stop_server.ps1", script)
        self.assertIn("Resource release failed", script)
        self.assertIn("try", script)
        self.assertIn("LocalOCR resources released", script)

    def test_ocr_smart_wrapper_exists(self) -> None:
        self.assertTrue((ROOT / "ocr_smart.ps1").exists())

    def test_ocr_smart_triage_does_not_require_path(self) -> None:
        script_path = self._windows_script_path(ROOT / "ocr_smart.ps1")
        executable = "pwsh" if os.name == "nt" else "pwsh.exe"
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
                "-TriageOnly",
                "-Port",
                "1",
            ],
            check=False,
            capture_output=True,
            text=False,
            timeout=20,
        )

        stdout = _decode_process_output(completed.stdout)
        stderr = _decode_process_output(completed.stderr)
        self.assertEqual(completed.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "triage_only")
        self.assertEqual(payload["route_reason"], "not_applicable_without_path")

    def test_ocr_smart_normal_mode_reports_missing_path_as_json(self) -> None:
        script_path = self._windows_script_path(ROOT / "ocr_smart.ps1")
        executable = "pwsh" if os.name == "nt" else "pwsh.exe"
        completed = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_path,
            ],
            check=False,
            capture_output=True,
            text=False,
            timeout=20,
        )

        stdout = _decode_process_output(completed.stdout)
        stderr = _decode_process_output(completed.stderr)
        self.assertEqual(completed.returncode, 0, stderr)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "missing_path")

    def test_ocr_smart_finalizes_child_exit_state(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("ReadToEndAsync", script)
        self.assertIn("WaitForExit(5000)", script)
        self.assertIn("Wait(1000)", script)
        self.assertIn("$process.Refresh()", script)
        self.assertIn("$ProgressPreference = 'SilentlyContinue'", script)

    def test_ocr_smart_uses_smart_router_v2_preview(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("Resolve-SmartRoutePreview", script)
        self.assertIn('".pdf"', script)
        self.assertIn('$RequestedEngine -ne "auto"', script)
        self.assertIn("pdf_plain_text_prefers_ocr", script)
        self.assertNotIn("pdf_complex_layout_prefers_vl", script)
        self.assertNotIn("$complexKeywords", script)
        self.assertNotIn("simple_pdf_prefers_ocr", script)

    def test_ocr_smart_passes_requested_engine_to_api_router(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("-Engine $(Quote-PowerShellString $Engine)", script)
        self.assertNotIn("-Engine $(Quote-PowerShellString $route.engine)", script)

    def test_ocr_smart_has_outer_timeout_and_compact_timeout_json(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("[int]$OuterTimeoutSec = 330", script)
        self.assertIn("[int]$ExecutionTimeoutSec = 300", script)
        self.assertIn("-ExecutionTimeoutSec $ExecutionTimeoutSec", script)
        self.assertIn("WaitForExit($TimeoutSec * 1000)", script)
        self.assertIn("-TimeoutSec $OuterTimeoutSec", script)
        self.assertIn("client_timeout", script)
        self.assertIn("do_not_blindly_retry", script)

    def test_ocr_smart_uses_health_active_jobs_as_the_only_activity_source(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("active_jobs", script)
        self.assertIn("health_active_jobs_unavailable", script)
        self.assertIn("active_localocr_task", script)
        self.assertNotIn("pgrep", script)

    def test_ocr_smart_does_not_infer_activity_from_process_names(self) -> None:
        script = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertNotIn("Get-LocalOcrActiveTasks", script)
        self.assertNotIn("Process.GetProcesses", script)

    def test_api_wrappers_accept_model_profile_override(self) -> None:
        once = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")
        smart = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn("[string]$Model", once)
        self.assertIn("body.model", once)
        self.assertIn("[string]$Model", smart)
        self.assertIn("-Model", smart)
        self.assertIn("requested_model", smart)

    def test_api_wrappers_accept_structure_engine(self) -> None:
        once = (ROOT / "ocr_once.ps1").read_text(encoding="utf-8")
        smart = (ROOT / "ocr_smart.ps1").read_text(encoding="utf-8")

        self.assertIn('[ValidateSet("auto", "ocr", "vl", "structure")]', once)
        self.assertIn('[ValidateSet("auto", "ocr", "vl", "structure")]', smart)


if __name__ == "__main__":
    unittest.main()
