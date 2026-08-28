from __future__ import annotations

import json
import os
import psutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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


class FakeLocalOcr:
    def __init__(
        self,
        *,
        health: dict[str, Any] | None = None,
        response_status: int = 200,
        response_payload: dict[str, Any] | None = None,
        health_after_calls: int | None = None,
        health_after: dict[str, Any] | None = None,
        post_delay_sec: float = 0,
    ) -> None:
        self.health = health or {
            "ok": True,
            "service": "localocr",
            "api_version": "0.6.0",
            "active_jobs_count": 0,
            "active_jobs": [],
            "gpu": None,
            "loaded_engines": [],
            "loaded_models": [],
        }
        self.response_status = response_status
        self.response_payload = response_payload or {
            "ok": True,
            "count": 1,
            "results": [],
        }
        self.health_after_calls = health_after_calls
        self.health_after = health_after
        self.post_delay_sec = post_delay_sec
        self.health_calls = 0
        self.post_count = 0
        self.post_bodies: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    # The smart wrapper may intentionally terminate its HTTP
                    # client at the outer timeout while this fake request is
                    # still sleeping.
                    pass

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/health":
                    self._write_json(404, {"detail": "not found"})
                    return
                owner.health_calls += 1
                payload = owner.health
                if (
                    owner.health_after_calls is not None
                    and owner.health_calls >= owner.health_after_calls
                    and owner.health_after is not None
                ):
                    payload = owner.health_after
                self._write_json(200, payload)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.post_count += 1
                owner.post_bodies.append(json.loads(body.decode("utf-8")))
                if owner.post_delay_sec:
                    time.sleep(owner.post_delay_sec)
                self._write_json(owner.response_status, owner.response_payload)

        return Handler

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> "FakeLocalOcr":
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class WindowsWrapperBehaviorTest(unittest.TestCase):
    def test_all_entrypoints_reject_remote_hosts_before_work(self):
        for script in ("ocr_once.ps1", "ocr_smart.ps1", "start_server.ps1", "stop_server.ps1"):
            with self.subTest(script=script):
                arguments = ["ignored.png"] if script.startswith("ocr_") else []
                completed = self._run_wrapper(script, *arguments, "-HostAddress", "198.51.100.10", "-Port", "9", timeout=10)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("HostAddress", completed.stderr)
                self.assertIn("127.0.0.1", completed.stderr)
                self.assertIn("198.51.100.10", completed.stderr)

    def _windows_script_path(self, path: Path) -> str:
        if os.name == "nt":
            return str(path)
        return subprocess.run(
            ["wslpath", "-w", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _run_wrapper(self, script: str, *arguments: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
        raw = subprocess.run(
            [
                "pwsh" if os.name == "nt" else "pwsh.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self._windows_script_path(ROOT / script),
                *arguments,
            ],
            check=False,
            capture_output=True,
            text=False,
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            raw.args,
            raw.returncode,
            _decode_process_output(raw.stdout),
            _decode_process_output(raw.stderr),
        )

    def _payload(self, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        self.assertTrue(completed.stdout.strip(), completed.stderr)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            start = completed.stdout.find("{")
            end = completed.stdout.rfind("}")
            if start >= 0 and end >= start:
                try:
                    return json.loads(completed.stdout[start : end + 1])
                except json.JSONDecodeError:
                    pass
            self.fail(f"wrapper did not return JSON: {exc}; stdout={completed.stdout!r}; stderr={completed.stderr!r}")

    def _unused_windows_port(self) -> int:
        executable = "pwsh" if os.name == "nt" else "pwsh.exe"
        port_command = "$listener=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Any,0);$listener.Start();try{$listener.LocalEndpoint.Port}finally{$listener.Stop()}"
        wsl_probe = (
            "import json,psutil,sys;"
            "port=int(sys.argv[1]);"
            "print(json.dumps(any(c.status==psutil.CONN_LISTEN and c.laddr and c.laddr.port==port "
            "for c in psutil.net_connections(kind='tcp'))))"
        )
        for _ in range(8):
            selected = subprocess.run(
                [executable, "-NoProfile", "-Command", port_command],
                check=False,
                capture_output=True,
                text=False,
                timeout=10,
            )
            self.assertEqual(selected.returncode, 0, _decode_process_output(selected.stderr))
            port = int(_decode_process_output(selected.stdout).strip())
            probe = subprocess.run(
                [
                    "wsl.exe",
                    "-d",
                    "Ubuntu",
                    "-e",
                    "/root/localocr-venv/bin/python",
                    "-c",
                    wsl_probe,
                    str(port),
                ],
                check=False,
                capture_output=True,
                text=False,
                timeout=10,
            )
            self.assertEqual(probe.returncode, 0, _decode_process_output(probe.stderr))
            self.assertEqual(_decode_process_output(probe.stdout).strip().splitlines()[-1], "false")
            return port
        self.fail("could not select a port free in both Windows and WSL")

    def test_windows_codepage_stderr_is_decoded_without_loss(self) -> None:
        self.assertEqual(_decode_process_output("失败".encode("gb18030")), "失败")
        self.assertEqual(_decode_process_output("失败".encode("utf-16")), "失败")

    def test_ocr_once_preserves_http_detail_and_execution_timeout(self) -> None:
        fake = FakeLocalOcr(
            response_status=400,
            response_payload={
                "ok": False,
                "status": "failed",
                "error_code": "input",
                "detail": "GpuBrokerConflict: request rejected for test",
                "job_key": "job-key-400",
            },
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_once.ps1",
                r"C:\input folder\sample.png",
                "-Port",
                str(fake.port),
                "-TimeoutSec",
                "5",
                "-ExecutionTimeoutSec",
                "17",
            )

        payload = self._payload(completed)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "input")
        self.assertEqual(payload["detail"], "GpuBrokerConflict: request rejected for test")
        self.assertEqual(payload["http_status"], 400)
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(fake.post_bodies[0]["timeout_sec"], 17)

    def test_ocr_once_distinguishes_http_transport_timeout(self) -> None:
        fake = FakeLocalOcr(
            response_payload={"ok": True, "count": 1, "results": []},
            post_delay_sec=3,
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_once.ps1",
                r"C:\input folder\slow.png",
                "-Port",
                str(fake.port),
                "-TimeoutSec",
                "1",
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "client_timeout")
        self.assertEqual(payload["error_code"], "client_timeout")
        self.assertEqual(payload["recommendation"], "do_not_blindly_retry")

    def test_ocr_once_reports_cleanup_failure_without_hiding_ocr_result(self) -> None:
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "service": "localocr",
                "server_pid": 42424242,
                "server_start_time": 1.0,
                "active_jobs": [],
                "active_jobs_count": 0,
            }
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_once.ps1",
                r"C:\input folder\sample.png",
                "-Port",
                str(fake.port),
                "-TimeoutSec",
                "5",
                "-StopAfter",
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "cleanup_failed")
        self.assertEqual(payload["error_code"], "resource_release_failed")
        self.assertTrue(payload["ocr_result"]["ok"])
        self.assertNotIn("resources released", completed.stdout.lower())

    def test_smart_relays_busy_job_and_does_not_collapse_http_json(self) -> None:
        fake = FakeLocalOcr(
            response_status=409,
            response_payload={
                "ok": False,
                "status": "active_localocr_task",
                "error_code": "gpu_busy",
                "detail": "GPU lease is held by job-key-busy",
                "job_id": "job-id-busy",
                "job_key": "job-key-busy",
                "recommendation": "do_not_blindly_retry",
                "active_jobs": [],
            },
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_smart.ps1",
                r"C:\input folder\sample.png",
                "-Port",
                str(fake.port),
                "-TimeoutSec",
                "5",
                "-ExecutionTimeoutSec",
                "19",
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 75)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "active_localocr_task")
        self.assertEqual(payload["error_code"], "gpu_busy")
        self.assertEqual(payload["detail"], "GPU lease is held by job-key-busy")
        self.assertEqual(payload["job_id"], "job-id-busy")
        self.assertEqual(payload["job_key"], "job-key-busy")
        self.assertEqual(payload["smart"]["client_exit_code"], 75)
        self.assertEqual(fake.post_bodies[0]["timeout_sec"], 19)

    def test_smart_preserves_quoted_path_and_model_arguments(self) -> None:
        fake = FakeLocalOcr()
        with fake:
            completed = self._run_wrapper(
                "ocr_smart.ps1",
                r"C:\用户's folder\sample image.png",
                "-Model",
                "profile with spaces",
                "-Port",
                str(fake.port),
                "-ExecutionTimeoutSec",
                "23",
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual(fake.post_count, 1)
        self.assertEqual(fake.post_bodies[0]["path"], r"C:\用户's folder\sample image.png")
        self.assertEqual(fake.post_bodies[0]["model"], "profile with spaces")
        self.assertEqual(fake.post_bodies[0]["timeout_sec"], 23)

    def test_smart_blocks_on_health_active_job_before_post(self) -> None:
        active_job = {
            "job_id": "job-id-running",
            "job_key": "job-key-running",
            "status": "running",
            "stage": "vl",
            "engine": "vl",
            "model_id": "paddleocr-vl-1.6",
            "source_file": "/tmp/running.pdf",
            "started_at": "2026-08-27T10:00:00Z",
            "updated_at": "2026-08-27T10:00:01Z",
            "timeout_sec": 300,
            "deadline_at": "2026-08-27T10:05:00Z",
        }
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "service": "localocr",
                "api_version": "0.6.0",
                "active_jobs_count": 1,
                "active_jobs": [active_job],
                "gpu": None,
                "loaded_engines": [],
                "loaded_models": [],
            }
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_smart.ps1",
                r"C:\input folder\sample.png",
                "-Port",
                str(fake.port),
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 75)
        self.assertEqual(payload["status"], "active_localocr_task")
        self.assertEqual(payload["job_id"], "job-id-running")
        self.assertEqual(payload["job_key"], "job-key-running")
        self.assertEqual(payload["active_jobs"][0]["deadline_at"], "2026-08-27T10:05:00Z")
        self.assertEqual(fake.post_count, 0)

    def test_smart_does_not_treat_legacy_health_as_idle(self) -> None:
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "gpu": None,
                "loaded_engines": [],
                "loaded_models": [],
            }
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_smart.ps1",
                r"C:\input folder\sample.png",
                "-Port",
                str(fake.port),
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(payload["status"], "readiness_unknown")
        self.assertEqual(payload["error_code"], "health_active_jobs_unavailable")
        self.assertEqual(payload["active_state"], "unknown")
        self.assertEqual(fake.post_count, 0)

    def test_smart_timeout_returns_active_job_location_without_retry(self) -> None:
        active_job = {
            "job_id": "job-id-timeout",
            "job_key": "job-key-timeout",
            "status": "running",
            "stage": "vl",
            "engine": "vl",
            "model_id": "paddleocr-vl-1.6",
            "source_file": "/tmp/timeout.pdf",
            "started_at": "2026-08-27T10:00:00Z",
            "updated_at": "2026-08-27T10:00:02Z",
            "timeout_sec": 300,
            "deadline_at": "2026-08-27T10:05:00Z",
        }
        idle_health = {
            "ok": True,
            "service": "localocr",
            "api_version": "0.6.0",
            "active_jobs_count": 0,
            "active_jobs": [],
            "gpu": None,
            "loaded_engines": [],
            "loaded_models": [],
        }
        running_health = dict(idle_health)
        running_health["active_jobs_count"] = 1
        running_health["active_jobs"] = [active_job]
        fake = FakeLocalOcr(
            health=idle_health,
            health_after_calls=3,
            health_after=running_health,
            post_delay_sec=5,
        )
        with fake:
            completed = self._run_wrapper(
                "ocr_smart.ps1",
                r"C:\input folder\slow.pdf",
                "-Port",
                str(fake.port),
                "-TimeoutSec",
                "10",
                "-OuterTimeoutSec",
                "2",
                "-ExecutionTimeoutSec",
                "300",
                timeout=30,
            )

        payload = self._payload(completed)
        self.assertEqual(completed.returncode, 124)
        self.assertEqual(payload["status"], "client_timeout")
        self.assertEqual(payload["job_id"], "job-id-timeout")
        self.assertEqual(payload["job_key"], "job-key-timeout")
        self.assertEqual(payload["active_jobs"][0]["deadline_at"], "2026-08-27T10:05:00Z")
        self.assertEqual(fake.post_count, 1)

    def test_stop_server_refuses_foreign_service_without_kill(self) -> None:
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "service": "not-localocr",
                "server_pid": 424242,
                "server_start_time": 1.0,
            }
        )
        with fake:
            completed = self._run_wrapper(
                "stop_server.ps1",
                "-Port",
                str(fake.port),
                "-WslTimeoutSec",
                "1",
            )

        self.assertNotEqual(completed.returncode, 0)
        output = f"{completed.stdout}\n{completed.stderr}"
        self.assertIn("Refusing to stop", output)
        self.assertNotIn("Stop command sent", output)

    def test_stop_server_refuses_health_identity_mismatch(self) -> None:
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "service": "localocr",
                "server_pid": 42424242,
                "server_start_time": 1.0,
                "active_jobs": [],
                "active_jobs_count": 0,
            }
        )
        with fake:
            completed = self._run_wrapper(
                "stop_server.ps1",
                "-Port",
                str(fake.port),
                "-WslTimeoutSec",
                "2",
            )

        self.assertNotEqual(completed.returncode, 0)
        output = f"{completed.stdout}\n{completed.stderr}"
        self.assertNotIn("API stopped", output)
        self.assertNotIn("Stop command sent", output)

    def test_release_resources_does_not_report_success_when_stop_fails(self) -> None:
        fake = FakeLocalOcr(
            health={
                "ok": True,
                "service": "not-localocr",
            }
        )
        with fake:
            completed = self._run_wrapper(
                "release_resources.ps1",
                "-Port",
                str(fake.port),
                "-WslTimeoutSec",
                "1",
            )

        self.assertNotEqual(completed.returncode, 0)
        output = f"{completed.stdout}\n{completed.stderr}"
        self.assertIn("Resource release failed", output)
        self.assertNotIn("resources released", output.lower())

    def test_stop_server_reports_unused_port_without_touching_pid_file(self) -> None:
        port = self._unused_windows_port()

        pid_path = ROOT / "_server" / "wsl-server.pid"
        before = pid_path.read_bytes() if pid_path.is_file() else None
        completed = self._run_wrapper(
            "stop_server.ps1",
            "-Port",
            str(port),
            "-WslTimeoutSec",
            "2",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = f"{completed.stdout}\n{completed.stderr}"
        self.assertIn("already stopped", output)
        self.assertNotIn("Stop failed", output)
        after = pid_path.read_bytes() if pid_path.is_file() else None
        self.assertEqual(after, before)

    def test_start_server_detached_launcher_closes_parent_pipe_and_keeps_child_output(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows CreateProcess handle-list test requires Windows PowerShell")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            log_path = self._windows_script_path(tmp_dir / "child.log").replace("'", "''")
            script_path = self._windows_script_path(ROOT / "start_server.ps1").replace("'", "''")
            command = f"""
$source = Get-Content -LiteralPath '{script_path}' -Raw
$match = [regex]::Match($source, "(?s)Add-Type -TypeDefinition @'\\r?\\n(?<code>.*?)\\r?\\n'@")
if (-not $match.Success) {{ throw 'detached launcher source was not found' }}
if (-not ('LocalOcrDetachedProcess' -as [type])) {{ Add-Type -TypeDefinition $match.Groups['code'].Value }}
$app = (Get-Process -Id $PID).Path
$childArgs = @('-NoProfile', '-Command', '[Console]::Out.WriteLine("child-stdout"); [Console]::Error.WriteLine("child-stderr"); Start-Sleep -Seconds 3')
$childPid = [LocalOcrDetachedProcess]::Start($app, $childArgs, (Get-Location).Path, '{log_path}')
Write-Output ('started=' + $childPid)
"""
            started_at = time.monotonic()
            completed = subprocess.run(
                [
                    "pwsh" if os.name == "nt" else "pwsh.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=6,
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("started=", completed.stdout)
            self.assertLess(elapsed, 3.5, "parent remained attached to the long-lived child pipe")
            self.assertNotIn("child-stdout", completed.stdout)
            self.assertNotIn("child-stderr", completed.stderr)
            child_pid = int(completed.stdout.strip().split("started=", 1)[1].splitlines()[0])

            child_log = Path(tmp) / "child.log"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child_log.is_file():
                    raw = child_log.read_text(encoding="utf-8", errors="replace")
                    if "child-stdout" in raw and "child-stderr" in raw:
                        break
                time.sleep(0.1)
            self.assertTrue(child_log.is_file())
            raw = child_log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("child-stdout", raw)
            self.assertIn("child-stderr", raw)
            try:
                psutil.Process(child_pid).wait(timeout=5)
            except psutil.NoSuchProcess:
                pass


if __name__ == "__main__":
    unittest.main()
