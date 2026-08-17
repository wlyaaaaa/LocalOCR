from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localocr.model_registry import select_model_profile
from localocr.service import OCRService, _request_variant, run_isolated_command


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


class FakeDifficultyEscalationService(OCRService):
    def __init__(self, *, tmp_dir: Path, job_dir: Path, scores: list[float]) -> None:
        super().__init__(device="gpu:0", tmp_dir=tmp_dir, job_dir=job_dir, probe_on_start=False)
        self.scores = scores
        self.ocr_calls = 0
        self.vl_calls = 0

    def process_file(
        self,
        path: Path,
        engine_choice: str = "auto",
        model_choice: str | None = None,
    ) -> dict:
        self.ocr_calls += 1
        return {
            "engine": "Fake OCR",
            "engine_key": "ocr",
            "model": "Fake OCR",
            "model_id": "ppocrv6-medium",
            "device": self.device,
            "pages": [
                {
                    "page_index": 0,
                    "blocks": [
                        {
                            "type": "text",
                            "text": f"line-{index}",
                            "score": score,
                            "order": index,
                        }
                        for index, score in enumerate(self.scores)
                    ],
                }
            ],
        }

    def _process_heavy_isolated(self, path: Path, output_dir: Path, profile) -> dict:
        self.vl_calls += 1
        self.assert_vl_profile = profile
        return {
            "engine": "Fake VL",
            "engine_key": "vl",
            "model": "Fake VL",
            "model_id": "paddleocr-vl-1.6",
            "device": self.device,
            "pages": [
                {
                    "page_index": 0,
                    "blocks": [{"type": "text", "text": "recovered", "score": None, "order": 0}],
                }
            ],
        }


class FakeEmptyResultService(OCRService):
    def __init__(self, *, tmp_dir: Path, job_dir: Path) -> None:
        super().__init__(device="gpu:0", tmp_dir=tmp_dir, job_dir=job_dir, probe_on_start=False)
        self.vl_calls = 0

    def process_file(
        self,
        path: Path,
        engine_choice: str = "auto",
        model_choice: str | None = None,
    ) -> dict:
        return {
            "engine": "Fake OCR",
            "engine_key": "ocr",
            "model": "Fake OCR",
            "model_id": "ppocrv6-medium",
            "device": self.device,
            "pages": [{"page_index": 0, "blocks": []}],
        }

    def _process_heavy_isolated(
        self,
        path: Path,
        output_dir: Path,
        profile,
        *,
        request_hash: str | None = None,
    ) -> dict:
        self.vl_calls += 1
        return {
            "engine": "Fake VL",
            "engine_key": "vl",
            "model": "Fake VL",
            "model_id": "paddleocr-vl-1.6",
            "device": self.device,
            "pages": [{"page_index": 0, "blocks": []}],
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
            self.assertEqual(second["results"][0]["route"]["effective_engine"], "ocr")
            self.assertEqual(service.calls, 1)

    def test_service_routes_auto_plain_pdf_to_ocr_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "address-confirmation.pdf"
            source.write_bytes(b"%PDF-1.7 plain scan")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")

            response = service.process_inputs([source], engine_choice="auto", out_dir=root / "out")

            route = response["results"][0]["route"]
            self.assertEqual(route["requested_engine"], "auto")
            self.assertEqual(route["effective_engine"], "ocr")
            self.assertEqual(route["reason"], "pdf_plain_text_prefers_ocr")
            self.assertEqual(route["model_id"], "ppocrv6-medium")
            self.assertEqual(service.calls, 1)

    def test_auto_low_confidence_ocr_escalates_to_vl_and_caches_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "difficult-chinese.png"
            source.write_bytes(b"image bytes")
            service = FakeDifficultyEscalationService(
                tmp_dir=root / "tmp",
                job_dir=root / "jobs",
                scores=[0.99, 0.41, 0.32, 0.96],
            )

            first = service.process_inputs([source], engine_choice="auto", out_dir=root / "out")
            second = service.process_inputs([source], engine_choice="auto", out_dir=root / "out")

            result = first["results"][0]
            route = result["route"]
            self.assertEqual(result["engine_key"], "vl")
            self.assertEqual(result["model_id"], "paddleocr-vl-1.6")
            self.assertTrue(route["escalated"])
            self.assertEqual(route["initial_engine"], "ocr")
            self.assertEqual(route["effective_engine"], "vl")
            self.assertEqual(route["reason"], "ocr_result_difficulty_prefers_vl")
            self.assertIn("low_score_ratio", route["difficulty"]["reasons"])
            self.assertEqual(route["escalation"]["from_model_id"], "ppocrv6-medium")
            self.assertEqual(route["escalation"]["to_model_id"], "paddleocr-vl-1.6")
            self.assertEqual(first["results"][0]["cache_status"], "stored")
            self.assertEqual(second["results"][0]["cache_status"], "cache_hit")
            self.assertEqual(service.ocr_calls, 1)
            self.assertEqual(service.vl_calls, 1)

    def test_explicit_ocr_does_not_auto_escalate_low_confidence_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "explicit-ocr.png"
            source.write_bytes(b"image bytes")
            service = FakeDifficultyEscalationService(
                tmp_dir=root / "tmp",
                job_dir=root / "jobs",
                scores=[0.20],
            )

            response = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")

            self.assertEqual(response["results"][0]["engine_key"], "ocr")
            self.assertEqual(response["results"][0]["route"]["effective_engine"], "ocr")
            self.assertNotIn("escalated", response["results"][0]["route"])
            self.assertEqual(service.ocr_calls, 1)
            self.assertEqual(service.vl_calls, 0)

    def test_auto_empty_ocr_and_vl_outputs_remain_indeterminate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Path(__file__).resolve().parent / "samples" / "probe_text.png"
            service = FakeEmptyResultService(tmp_dir=root / "tmp", job_dir=root / "jobs")

            first = service.process_inputs([source], engine_choice="auto", out_dir=root / "out")
            second = service.process_inputs([source], engine_choice="auto", out_dir=root / "out")

            result = first["results"][0]
            self.assertEqual(result["objective_outcome"], "indeterminate")
            self.assertEqual(result["execution_status"], "completed")
            self.assertTrue(result["route"]["escalated"])
            self.assertEqual(result["route"]["effective_engine"], "vl")
            self.assertEqual(first["results"][0]["cache_status"], "stored")
            self.assertEqual(second["results"][0]["cache_status"], "cache_hit")
            self.assertEqual(service.vl_calls, 1)

    def test_service_returns_active_job_without_running_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")
            profile = select_model_profile(source, engine_choice="ocr")
            request = service.job_registry.build_request(
                source,
                profile,
                root / "out",
                request_variant=_request_variant("ocr", None),
            )
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
            self.assertEqual(response["route"]["effective_engine"], "ocr")
            self.assertEqual(service.calls, 0)

    def test_same_stem_projections_are_isolated_and_old_cache_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "first" / "same.png"
            second_source = root / "second" / "same.png"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_bytes(b"first image bytes")
            second_source.write_bytes(b"second image bytes")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")

            first = service.process_inputs([first_source], engine_choice="ocr", out_dir=root / "out")
            second = service.process_inputs([second_source], engine_choice="ocr", out_dir=root / "out")

            first_files = first["results"][0]["output_files"]
            second_files = second["results"][0]["output_files"]
            self.assertNotEqual(first_files["objective"], second_files["objective"])
            self.assertNotEqual(first_files["canonical_json"], second_files["canonical_json"])

            # The legacy same-stem display JSON was overwritten by the second
            # source, so the first job must fail artifact-hash validation and
            # rerun rather than report a false cache hit.
            again = service.process_inputs([first_source], engine_choice="ocr", out_dir=root / "out")
            self.assertEqual(again["results"][0]["cache_status"], "stored")

    def test_modern_cache_requires_complete_nonempty_artifact_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            service = FakeCacheService(tmp_dir=root / "tmp", job_dir=root / "jobs")

            first = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")
            job_key = first["results"][0]["job_key"]
            manifest_path = root / "jobs" / f"{job_key}.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("output_file_sha256", None)
            manifest.pop("output_file_size_bytes", None)
            manifest["result"].pop("output_file_sha256", None)
            manifest["result"].pop("output_file_size_bytes", None)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            again = service.process_inputs([source], engine_choice="ocr", out_dir=root / "out")
            self.assertEqual(again["results"][0]["cache_status"], "stored")
            self.assertEqual(service.calls, 2)

    def test_service_treats_structure_as_isolated_heavy_engine(self) -> None:
        service_source = (Path(__file__).resolve().parent.parent / "localocr" / "service.py").read_text(
            encoding="utf-8"
        )
        cli_source = (Path(__file__).resolve().parent.parent / "localocr" / "cli.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("HEAVY_ISOLATED_ENGINES", service_source)
        self.assertIn('"vl"', service_source)
        self.assertIn('"structure"', service_source)
        self.assertIn("profile.engine not in HEAVY_ISOLATED_ENGINES", service_source)
        self.assertIn("_process_selected_profile", service_source)
        self.assertIn("--broker-lease-held-by-parent", service_source)
        self.assertIn("broker_lease_held_by_parent", cli_source)

    def test_heavy_child_reads_request_isolated_json_for_same_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "incoming" / "same.png"
            source.parent.mkdir()
            source.write_bytes(b"image bytes")
            output_dir = root / "out"
            request_hash = "a" * 64
            profile = select_model_profile(source, engine_choice="vl")
            canonical_json = output_dir / f"same.{request_hash[:32]}.json"
            legacy_json = output_dir / "same.json"
            output_dir.mkdir()
            canonical_json.write_text(json.dumps({"marker": "canonical"}), encoding="utf-8")
            legacy_json.write_text(json.dumps({"marker": "legacy"}), encoding="utf-8")
            service = OCRService(
                device="gpu:0",
                tmp_dir=root / "tmp",
                job_dir=root / "jobs",
                probe_on_start=False,
            )

            with patch(
                "localocr.service.run_isolated_command",
                return_value=CompletedProcess([], 0, "", ""),
            ):
                result = service._process_heavy_isolated(
                    source,
                    output_dir,
                    profile,
                    request_hash=request_hash,
                )

            self.assertEqual(result["marker"], "canonical")

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
