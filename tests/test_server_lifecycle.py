from __future__ import annotations

import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from localocr import server
from localocr.gpu_broker import GpuBrokerConflict, GpuBrokerError, GpuBrokerLeaseLost
from localocr.runtime import ExecutionError


class FakeService:
    def __init__(self):
        self.calls = []
        self.error = None
        self.response = {"ok": True, "count": 0, "results": []}
        self.active_jobs = []
        self.loaded_engines, self.loaded_models = [], []
        self.gpu_summary = None
        self.terminal_persistence_failure = None
        self._runtime = SimpleNamespace(pid=None, peak_memory_bytes=0, memory_limit_bytes=30_000_000_000)
        self.job_registry = SimpleNamespace(read_status=lambda key: {"ok": False, "status": "not_found", "job_key": key})

    def process_inputs(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response

    def cancel(self, key):
        return any(item["job_key"] == key for item in self.active_jobs)

    def close(self):
        pass


class ServerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.patcher = patch.object(server, "_service", self.service)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client = TestClient(server.app, base_url="http://127.0.0.1:18665")

    def test_untrusted_host_is_rejected_before_work(self):
        for host in ("evil.example:18665", "127.0.0.1.evil.example", "evil.example"):
            self.assertEqual(self.client.get("/health", headers={"Host": host}).status_code, 400)
            self.assertEqual(self.client.post("/ocr/path", json={"path": "sample.png"}, headers={"Host": host}).status_code, 400)
        self.assertEqual(self.service.calls, [])

    def test_json_body_requires_json_content_type(self):
        for headers in ({}, {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
            response = self.client.post("/ocr/path", content='{"path":"sample.png"}', headers=headers)
            self.assertEqual(response.status_code, 415)
        self.assertEqual(self.service.calls, [])

    def test_foreign_browser_origin_is_rejected_for_every_entry(self):
        self.service.active_jobs = [{"job_key": "active"}]
        for origin in ("https://evil.example", "null", "http://127.0.0.1:9999", "http://127.0.0.1:invalid"):
            for path in ("/ocr/path", "/ocr/file", "/jobs/active/cancel"):
                response = self.client.post(path, headers={"Origin": origin}, content=b"")
                self.assertEqual(response.status_code, 403, (path, origin))
            self.assertEqual(self.client.get("/health", headers={"Origin": origin}).status_code, 403)
        self.assertEqual(self.service.calls, [])

    def test_native_and_same_origin_clients_keep_json_and_upload_support(self):
        response = self.client.post("/ocr/path", content='{"path":"sample.png"}',
                                    headers={"Origin": "http://127.0.0.1:18665", "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/ocr/file", files={"file": ("sample.png", b"synthetic", "image/png")})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.service.calls), 2)

    def test_localhost_remains_an_allowed_host(self):
        self.assertEqual(self.client.get("/health", headers={"Host": "localhost:18665"}).status_code, 200)

    def test_health_is_lightweight_and_distinguishes_gpu_not_yet_probed(self):
        response = self.client.get("/health")
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["service"], "localocr")
        self.assertEqual(body["gpu_status"], "not_probed")
        self.assertEqual(body["active_jobs"], [])
        self.assertGreater(body["server_pid"], 0)
        self.assertGreater(body["server_start_time"], 0)
        self.assertEqual(self.service.calls, [])

    def test_health_does_not_call_unpersisted_terminal_state_ready(self):
        self.service.terminal_persistence_failure = {"job_id": "a" * 16, "job_key": "a" * 64,
                                                     "original_detail": "private diagnostic"}
        body = self.client.get("/health").json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["service"], "localocr")
        self.assertEqual(body["readiness"], "job_state_persistence_failed")
        self.assertEqual(body["recovery_job"]["job_key"], "a" * 64)
        self.assertNotIn("private diagnostic", str(body))

    def test_request_execution_deadline_reaches_service(self):
        response = self.client.post("/ocr/path", json={"path": "sample.png", "timeout_sec": 17})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.calls[0][1]["timeout_sec"], 17)

    def test_invalid_execution_deadline_is_rejected_before_work(self):
        for timeout in (0, -1, 7201, "invalid"):
            response = self.client.post("/ocr/path", json={"path": "sample.png", "timeout_sec": timeout})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.calls, [])

    def test_busy_and_errors_keep_top_level_machine_readable_detail(self):
        failures = [
            (GpuBrokerConflict("busy", owner="chineseasr"), 409, "gpu_busy"),
            (GpuBrokerError("offline"), 503, "broker_unavailable"),
            (GpuBrokerLeaseLost("lost"), 503, "gpu_lease_lost"),
            (FileNotFoundError("missing"), 404, "input_not_found"),
            (ValueError("bad input"), 400, "invalid_request"),
            (RuntimeError("native crash"), 500, "runtime_error"),
        ]
        for error, status, code in failures:
            self.service.error = error
            response = self.client.post("/ocr/path", json={"path": "sample.png"})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error_code"], code)
            self.assertIn(str(error), response.json()["detail"])
            self.assertFalse(response.json()["ok"])

    def test_timeout_returns_exact_job_reference_and_partial_output(self):
        error = ExecutionError("execution_timeout", "deadline", http_status=504)
        error.context = {"job_key": "a" * 64, "job_id": "a" * 16, "partial_output_files": {"md": "partial.md"}}
        self.service.error = error
        response = self.client.post("/ocr/path", json={"path": "sample.png"})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["job_key"], "a" * 64)
        self.assertEqual(response.json()["partial_output_files"], {"md": "partial.md"})

    def test_known_active_job_returns_conflict_not_bad_request(self):
        self.service.response = {"ok": False, "status": "active_localocr_task", "job_key": "b" * 64}
        response = self.client.post("/ocr/path", json={"path": "sample.png"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["job_key"], "b" * 64)

    def test_cancel_only_targets_the_named_active_job(self):
        self.service.active_jobs = [{"job_key": "c" * 64}]
        self.assertEqual(self.client.post("/jobs/" + "c" * 64 + "/cancel").status_code, 202)
        self.assertEqual(self.client.post("/jobs/" + "d" * 64 + "/cancel").status_code, 404)

    def test_api_import_does_not_import_native_gpu_runtime(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import localocr.server,sys; assert 'paddle' not in sys.modules; assert 'paddleocr' not in sys.modules"],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_cli_rejects_non_loopback_bind_before_starting_server(self):
        with patch.object(sys, "argv", ["localocr.server", "--host", "0.0.0.0"]), \
             patch("uvicorn.run") as run:
            with self.assertRaises(SystemExit) as raised:
                server.main()
        self.assertEqual(raised.exception.code, 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
