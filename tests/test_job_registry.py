from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localocr.model_registry import ModelProfile


def fake_profile(profile_id: str = "ppocrv6-medium", engine: str = "ocr") -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        engine=engine,
        adapter="localocr.engines.ppocrv6:PPOCRv6Engine",
        display_name="Fake OCR",
        result_engine_name="Fake OCR",
        backend="fake",
        pipeline_version="fake",
        capabilities=("plain_ocr",),
        options={},
    )


class JobRegistryTest(unittest.TestCase):
    def test_job_key_tracks_file_content_profile_and_output_dir(self) -> None:
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"same image bytes")
            output_dir = root / "out"
            registry = JobRegistry(root / "jobs")

            first = registry.build_request(source, fake_profile(), output_dir)
            second = registry.build_request(source, fake_profile(), output_dir)
            self.assertEqual(first.job_key, second.job_key)

            source.write_bytes(b"changed image bytes")
            changed = registry.build_request(source, fake_profile(), output_dir)
            self.assertNotEqual(first.job_key, changed.job_key)

            different_output = registry.build_request(source, fake_profile(), root / "other-out")
            self.assertNotEqual(changed.job_key, different_output.job_key)

    def test_completed_job_returns_cache_hit_when_outputs_exist(self) -> None:
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            output_dir = root / "out"
            output_dir.mkdir()
            output_files = {
                "txt": str(output_dir / "sample.txt"),
                "md": str(output_dir / "sample.md"),
                "json": str(output_dir / "sample.json"),
            }
            for path in output_files.values():
                Path(path).write_text("cached", encoding="utf-8")

            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), output_dir)
            claim = registry.try_claim(request)
            self.assertEqual(claim.kind, "run")
            registry.complete(claim, {"output_files": output_files, "pages": []})
            registry.release(claim)

            cached = registry.try_claim(request)

            self.assertEqual(cached.kind, "cache_hit")
            self.assertEqual(cached.response["cache_status"], "cache_hit")
            self.assertEqual(cached.response["job_key"], request.job_key)
            self.assertEqual(cached.response["output_files"], output_files)
            self.assertEqual(registry.read_status(request.job_key)["status"], "completed")

    def test_running_claim_returns_active_without_second_lock(self) -> None:
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"image bytes")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile("paddleocr-vl-1.6", "vl"), root / "out")
            claim = registry.try_claim(request)
            self.assertEqual(claim.kind, "run")

            active = registry.try_claim(request)

            self.assertEqual(active.kind, "active")
            self.assertEqual(active.response["status"], "active_localocr_task")
            self.assertEqual(active.response["recommendation"], "do_not_blindly_retry")
            self.assertEqual(active.response["job_key"], request.job_key)
            registry.release(claim)


if __name__ == "__main__":
    unittest.main()
