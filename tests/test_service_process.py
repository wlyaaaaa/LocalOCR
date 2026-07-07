from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import TimeoutExpired

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localocr.model_registry import select_model_profile
from localocr.service import OCRService, run_isolated_command


class FakeCacheService(OCRService):
    def __init__(self, *, tmp_dir: Path, job_dir: Path) -> None:
        super().__init__(device="gpu:0", tmp_dir=tmp_dir, job_dir=job_dir, probe_on_start=False)
        self.calls = 0

    def process_file(
        self,
        path: Path,
        engine_choice: str = "auto",
        model_choice: str | None = None,
    ) -> dict:
        self.calls += 1
        return {
            "engine": "Fake OCR",
            "engine_key": "ocr",
            "model": "Fake OCR",
            "model_id": "ppocrv6-medium",
            "device": self.device,
            "pages": [{"page_index": 0, "blocks": [{"type": "text", "text": path.name, "order": 0}]}],
        }


class IsolatedProcessTest(unittest.TestCase):
    def test_service_reuses_completed_job_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")

            first = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")
            second = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")

            self.assertTrue(first["ok"])
            self.assertEqual(first["results"][0]["cache_status"], "stored")
            self.assertTrue(second["ok"])
            self.assertEqual(second["results"][0]["cache_status"], "cache_hit")
            self.assertEqual(second["results"][0]["output_files"], first["results"][0]["output_files"])
            self.assertEqual(service.calls, 1)

    def test_service_returns_active_job_without_running_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")
            profile = select_model_profile(source, engine_choice="ocr")
            request = service.job_registry.build_request(source, profile, root / "out")
            claim = service.job_registry.try_claim(request)
            self.assertEqual(claim.kind, "run")

            try:
                response = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")
            finally:
                service.job_registry.release(claim)

            self.assertFalse(response["ok"])
            self.assertEqual(response["status"], "active_localocr_task")
            self.assertEqual(response["recommendation"], "do_not_blindly_retry")
            self.assertEqual(response["job_key"], request.job_key)
            self.assertEqual(service.calls, 0)

    def test_service_treats_structure_as_isolated_heavy_engine(self) -> None:
        service_source = (Path(__file__).resolve().parent.parent / "localocr" / "service.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("HEAVY_ISOLATED_ENGINES", service_source)
        self.assertIn('"vl"', service_source)
        self.assertIn('"structure"', service_source)
        self.assertIn("profile.engine in HEAVY_ISOLATED_ENGINES", service_source)

    def test_timeout_kills_child_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group cleanup is verified in WSL/Linux")

        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            parent_code = (
                "import pathlib, subprocess, sys, time; "
                f"pid_file = pathlib.Path({str(pid_file)!r}); "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pid_file.write_text(str(child.pid), encoding='utf-8'); "
                "time.sleep(60)"
            )

            with self.assertRaises(TimeoutExpired):
                run_isolated_command(
                    [sys.executable, "-c", parent_code],
                    cwd=Path(tmp),
                    timeout_sec=1,
                )

            deadline = time.time() + 5
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "test child process did not start")

            child_pid = int(pid_file.read_text(encoding="utf-8"))
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)

            self.fail(f"child process {child_pid} survived isolated-command timeout")


if __name__ == "__main__":
    unittest.main()
