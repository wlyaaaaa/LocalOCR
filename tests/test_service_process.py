from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from localocr.model_registry import select_model_profile
from localocr.runtime import ExecutionError
from localocr.service import OCRService, _request_variant


class FakeService(OCRService):
    def __init__(
        self,
        root: Path,
        *,
        device="cpu",
        scores=None,
        empty=False,
        fail_vl=False,
    ):
        super().__init__(
            device=device,
            tmp_dir=root / "tmp",
            job_dir=root / "jobs",
            probe_on_start=False,
            gpu_lease_factory=lambda _owner: nullcontext(),
        )
        self.calls = []
        self.scores = [0.99] if scores is None else scores
        self.empty, self.fail_vl = empty, fail_vl

    def process_file(self, path, engine_choice="auto", model_choice=None):
        self.calls.append((engine_choice, path))
        if engine_choice == "vl" and self.fail_vl:
            raise ExecutionError(
                "execution_timeout", "fake worker timed out", http_status=504
            )
        blocks = (
            []
            if self.empty
            else [
                {
                    "type": "text",
                    "text": f"line-{index}",
                    "score": score,
                    "order": index,
                }
                for index, score in enumerate(
                    self.scores if engine_choice == "ocr" else [0.99]
                )
            ]
        )
        return {
            "engine": "Fake",
            "engine_key": engine_choice,
            "model": "Fake",
            "model_id": model_choice,
            "device": self.device,
            "pages": [{"page_index": 0, "blocks": blocks}],
        }


class ServiceProcessTests(unittest.TestCase):
    def source(self, root: Path, name="sample.png", data=b"image bytes") -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_completed_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)
            first = service.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )
            second = service.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )
            self.assertEqual(first["results"][0]["cache_status"], "stored")
            self.assertEqual(second["results"][0]["cache_status"], "cache_hit")
            self.assertEqual(
                first["results"][0]["display_summary"]["status"], "text_detected"
            )
            self.assertEqual(
                second["results"][0]["display_summary"],
                first["results"][0]["display_summary"],
            )
            self.assertEqual(len(service.calls), 1)
            self.assertEqual(service.calls[0][1].name, source.name)
            self.assertNotEqual(service.calls[0][1], source)

    def test_request_variant_versions_display_projection(self):
        variant = _request_variant("ocr", None, device="gpu:0")
        self.assertIn("output=display-summary-v1", variant)

    def test_auto_routes_and_escalates_low_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = (
                self.source(root),
                FakeService(root, scores=[0.99, 0.32, 0.40]),
            )
            first = service.process_inputs([source], out_dir=root / "out")
            result = first["results"][0]
            self.assertEqual([call[0] for call in service.calls], ["ocr", "vl"])
            self.assertEqual(result["model_id"], "paddleocr-vl-1.6")
            self.assertTrue(result["route"]["escalated"])
            self.assertEqual(result["route"]["initial_engine"], "ocr")
            second = service.process_inputs([source], out_dir=root / "out")
            self.assertEqual(second["results"][0]["cache_status"], "cache_hit")
            self.assertEqual(len(service.calls), 2)

    def test_explicit_ocr_does_not_escalate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root, scores=[0.1])
            result = service.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertEqual([call[0] for call in service.calls], ["ocr"])
            self.assertEqual(result["engine_key"], "ocr")

    def test_empty_auto_result_does_not_certify_no_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = Path(__file__).parent / "samples" / "probe_text.png"
            service = FakeService(root, empty=True)
            result = service.process_inputs([source], out_dir=root / "out")["results"][
                0
            ]
            self.assertEqual(result["objective_outcome"], "indeterminate")
            self.assertTrue(result["route"]["escalated"])

    def test_same_stem_display_alias_cannot_invalidate_canonical_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self.source(root, "first/same.png", b"first")
            second = self.source(root, "second/same.png", b"second")
            service = FakeService(root)
            one = service.process_inputs(
                [first], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            two = service.process_inputs(
                [second], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertNotEqual(
                one["output_files"]["json"], two["output_files"]["json"]
            )
            again = service.process_inputs(
                [first], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertEqual(again["cache_status"], "cache_hit")
            self.assertEqual(len(service.calls), 2)

    def test_cache_requires_complete_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)
            first = service.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            manifest_path = root / "jobs" / f"{first['job_key']}.json"
            manifest = json.loads(manifest_path.read_text())
            for payload in (manifest, manifest["result"]):
                payload.pop("output_file_sha256", None)
                payload.pop("output_file_size_bytes", None)
            manifest_path.write_text(json.dumps(manifest))
            again = service.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertEqual(again["cache_status"], "stored")
            self.assertEqual(len(service.calls), 2)

    def test_device_is_bound_into_request_identity_and_sidecar_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.source(root)
            cpu = FakeService(root, device="cpu")
            gpu = FakeService(root, device="gpu:0")

            cpu_result = cpu.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            gpu_result = gpu.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]

            self.assertEqual(cpu_result["cache_status"], "stored")
            self.assertEqual(gpu_result["cache_status"], "stored")
            self.assertNotEqual(cpu_result["job_key"], gpu_result["job_key"])
            self.assertEqual(len(cpu.calls), 1)
            self.assertEqual(len(gpu.calls), 1)
            objective = json.loads(
                Path(gpu_result["output_files"]["objective"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                objective["identity"]["request_sha256"], gpu_result["job_key"]
            )

            gpu_cached = gpu.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertEqual(gpu_cached["cache_status"], "cache_hit")
            self.assertEqual(len(gpu.calls), 1)

            cuda_alias = FakeService(root, device="cuda:0")
            cuda_cached = cuda_alias.process_inputs(
                [source], engine_choice="ocr", out_dir=root / "out"
            )["results"][0]
            self.assertEqual(cuda_cached["cache_status"], "cache_hit")
            self.assertEqual(len(cuda_alias.calls), 0)

    def test_terminal_manifest_write_failure_retains_lock_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)

            def fail_processing(*_args, **_kwargs):
                raise ExecutionError("execution_timeout", "simulated OCR failure", http_status=504)

            service.process_file = fail_processing
            with patch.object(
                service.job_registry,
                "fail",
                side_effect=OSError("simulated terminal manifest write failure"),
            ):
                with self.assertRaises(ExecutionError) as raised:
                    service.process_inputs(
                        [source], engine_choice="ocr", out_dir=root / "out"
                    )

            error = raised.exception
            self.assertEqual(error.code, "job_state_persistence_failed")
            job_key = error.context["job_key"]
            self.assertEqual(error.context["original_error_code"], "execution_timeout")
            self.assertIn("terminal manifest write failure", error.context["persistence_error"])
            self.assertTrue((root / "jobs" / f"{job_key}.lock").exists())
            self.assertEqual(service.job_registry.read_status(job_key)["status"], "running")
            self.assertEqual(service.terminal_persistence_failure["job_key"], job_key)

            other = self.source(root, "other.png")
            with self.assertRaises(ExecutionError) as rejected:
                service.process_inputs([other], engine_choice="ocr", out_dir=root / "out")
            self.assertEqual(rejected.exception.code, "job_state_persistence_failed")
            self.assertEqual(rejected.exception.context["job_key"], job_key)
            self.assertEqual(len(list((root / "jobs").glob("*.json"))), 1)

    def test_duplicate_running_job_is_not_run_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)
            request = service.job_registry.build_request(
                source,
                select_model_profile(source, engine_choice="ocr"),
                root / "out",
                request_variant=_request_variant("ocr", None, device=service.device),
            )
            claim = service.job_registry.try_claim(request)
            try:
                response = service.process_inputs(
                    [source], engine_choice="ocr", out_dir=root / "out"
                )
                self.assertEqual(response["status"], "active_localocr_task")
                self.assertEqual(response["job_key"], request.job_key)
                self.assertEqual(service.calls, [])
            finally:
                service.job_registry.release(claim)

    def test_write_files_false_does_not_leave_a_job_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)
            result = service.process_inputs(
                [source], engine_choice="ocr", write_files=False
            )["results"][0]
            self.assertEqual(result["cache_status"], "not_written")
            self.assertNotIn("output_files", result)
            self.assertFalse((root / "jobs").exists())
            self.assertEqual(list((root / "tmp").iterdir()), [])

    def test_changed_source_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = self.source(root), FakeService(root)
            predict = service.process_file

            def mutate(*args):
                result = predict(*args)
                source.write_bytes(b"changed")
                return result

            service.process_file = mutate
            with self.assertRaisesRegex(ExecutionError, "Original source changed"):
                service.process_inputs(
                    [source], engine_choice="ocr", out_dir=root / "out"
                )
            self.assertFalse((root / "out").exists())
            self.assertEqual(list((root / "jobs").glob("*.lock")), [])
            self.assertEqual(service.active_jobs, [])

    def test_failed_escalation_preserves_initial_result_without_success_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, service = (
                self.source(root),
                FakeService(root, scores=[0.1], fail_vl=True),
            )
            with self.assertRaises(ExecutionError) as raised:
                service.process_inputs([source], out_dir=root / "out")
            key = raised.exception.context["job_key"]
            state = service.job_registry.read_status(key)
            self.assertEqual(state["status"], "failed")
            self.assertFalse(state["cache_available"])
            self.assertTrue(Path(state["partial_output_files"]["json"]).is_file())
            self.assertEqual(service.active_jobs, [])
            self.assertEqual(list((root / "jobs").glob("*.lock")), [])

    def test_different_request_is_busy_without_second_failed_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = self.source(root, "one.png"), self.source(root, "two.png")
            service = FakeService(root)
            entered, finish = threading.Event(), threading.Event()
            predict = service.process_file

            def wait(*args):
                entered.set()
                finish.wait(5)
                return predict(*args)

            service.process_file = wait
            thread = threading.Thread(
                target=service.process_inputs,
                args=([first],),
                kwargs={"engine_choice": "ocr", "out_dir": root / "out"},
            )
            thread.start()
            self.assertTrue(entered.wait(5))
            response = service.process_inputs(
                [second], engine_choice="ocr", out_dir=root / "out"
            )
            self.assertEqual(response["status"], "active_localocr_task")
            self.assertEqual(response["active_jobs_count"], 1)
            self.assertEqual(response["job_key"], service.active_jobs[0]["job_key"])
            finish.set()
            thread.join(5)
            self.assertEqual(len(list((root / "jobs").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
