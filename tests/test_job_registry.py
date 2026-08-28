from __future__ import annotations

import tempfile
import unittest
import json
import multiprocessing
import os
import time
from unittest.mock import patch

import psutil
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


def _concurrent_stale_claim(
    job_dir: str,
    source_path: str,
    output_dir: str,
    barrier,
    release,
    results,
) -> None:
    """Force two independent coordinators to enter the stale-takeover path."""

    from localocr.job_registry import JobRegistry

    registry = JobRegistry(job_dir)
    request = registry.build_request(source_path, fake_profile(), output_dir)
    try:
        barrier.wait(timeout=10)
        claim = registry.try_claim(request)
        results.put({"kind": claim.kind, "execution_id": claim.execution_id})
        if claim.kind == "run":
            release.wait(10)
            registry.release(claim)
    except BaseException as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})


def close_claim_fd(claim) -> None:
    """Compatibility helper for claims created before the short-lived-FD rule."""

    if claim.lock_fd is not None:
        os.close(claim.lock_fd)
        claim.lock_fd = None


class JobRegistryTest(unittest.TestCase):
    def test_legacy_plain_pid_lock_uses_narrow_reuse_proof(self):
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = root / "legacy.lock"
            lock.write_text(str(os.getpid()), encoding="utf-8")
            registry = JobRegistry(root)

            # A legacy PID without a start timestamp cannot be stolen merely
            # because it is old while the PID is still live.
            self.assertFalse(registry._lock_is_stale(lock, root / "legacy.json"))

            # If that same numeric PID's current process demonstrably began
            # after the lock, it is a reused PID and recovery is safe.
            old_lock_time = psutil.Process().create_time() - 10
            os.utime(lock, (old_lock_time, old_lock_time))
            self.assertTrue(registry._lock_is_stale(lock, root / "legacy.json"))

    def test_legacy_plain_pid_reuse_is_recovered_to_failed_terminal_state(self):
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), root / "out")
            claim = registry.try_claim(request)
            close_claim_fd(claim)
            claim.lock_path.write_text(str(os.getpid()), encoding="utf-8")
            old_lock_time = psutil.Process().create_time() - 10
            os.utime(claim.lock_path, (old_lock_time, old_lock_time))

            registry.recover_abandoned()

            self.assertFalse(claim.lock_path.exists())
            state = registry.read_status(request.job_key)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error_code"], "owner_exited")

    def test_dead_owner_lock_recovers_without_waiting_24_hours(self):
        from localocr.job_registry import JobRegistry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), root / "out")
            claim = registry.try_claim(request)
            close_claim_fd(claim)
            claim.lock_path.write_text(json.dumps({"owner_pid": 2_000_000_000, "owner_start_time": 0, "execution_id": claim.execution_id}))
            registry.recover_abandoned()
            self.assertFalse(claim.lock_path.exists())
            status = registry.read_status(request.job_key)
            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error_code"], "owner_exited")
            next_claim = registry.try_claim(request)
            self.assertEqual(next_claim.kind, "run")
            registry.release(next_claim)

    def test_old_but_live_owner_lock_is_not_stolen(self):
        from localocr.job_registry import JobRegistry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs", stale_after_sec=1)
            request = registry.build_request(source, fake_profile(), root / "out")
            claim = registry.try_claim(request)
            old = time.time() - 100
            os.utime(claim.lock_path, (old, old))
            registry.recover_abandoned()
            self.assertTrue(claim.lock_path.exists())
            self.assertEqual(registry.try_claim(request).kind, "active")
            registry.release(claim)

    def test_pid_reuse_does_not_preserve_stale_owner(self):
        from localocr.job_registry import JobRegistry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.mkdir(exist_ok=True)
            lock = root / "job.lock"
            lock.write_text(json.dumps({"owner_pid": os.getpid(), "owner_start_time": psutil.Process().create_time() - 100}))
            self.assertTrue(JobRegistry(root)._lock_is_stale(lock, root / "job.json"))

    def test_stale_claim_cannot_publish_over_replacement_execution(self):
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), root / "out")
            old_claim = registry.try_claim(request)
            close_claim_fd(old_claim)
            old_claim.lock_path.write_text(
                json.dumps(
                    {
                        "owner_pid": 2_000_000_000,
                        "owner_start_time": 0,
                        "execution_id": old_claim.execution_id,
                    }
                ),
                encoding="utf-8",
            )

            replacement = registry.try_claim(request)
            self.assertEqual(replacement.kind, "run")
            self.assertNotEqual(replacement.execution_id, old_claim.execution_id)

            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                registry.complete(old_claim, {"output_files": {}, "pages": []})
            with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                registry.fail(old_claim, RuntimeError("old worker failed"))

            running = registry.read_status(request.job_key)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["execution_id"], replacement.execution_id)

            registry.complete(replacement, {"output_files": {}, "pages": []})
            registry.release(replacement)

    def test_cross_process_stale_takeover_cannot_delete_replacement_lock(self):
        from localocr.job_registry import JobRegistry

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), root / "out")
            old_claim = registry.try_claim(request)
            close_claim_fd(old_claim)
            old_claim.lock_path.write_text(
                json.dumps(
                    {
                        "owner_pid": 2_000_000_000,
                        "owner_start_time": 0,
                        "execution_id": old_claim.execution_id,
                    }
                ),
                encoding="utf-8",
            )

            ctx = multiprocessing.get_context("spawn")
            barrier = ctx.Barrier(2)
            release = ctx.Event()
            results = ctx.Queue()
            workers = [
                ctx.Process(
                    target=_concurrent_stale_claim,
                    args=(str(root / "jobs"), str(source), str(root / "out"), barrier, release, results),
                )
                for _ in range(2)
            ]
            try:
                for worker in workers:
                    worker.start()
                observed = [results.get(timeout=15) for _ in workers]
                self.assertFalse([item for item in observed if "error" in item], observed)
                self.assertEqual(sorted(item["kind"] for item in observed), ["active", "run"])
                run = next(item for item in observed if item["kind"] == "run")
                manifest = registry.read_status(request.job_key)
                self.assertEqual(manifest["status"], "running")
                self.assertEqual(manifest["execution_id"], run["execution_id"])
                self.assertTrue(old_claim.lock_path.exists())
            finally:
                release.set()
                for worker in workers:
                    worker.join(10)
                    if worker.is_alive():
                        worker.kill()
                        worker.join(5)
                results.close()
                results.join_thread()
            self.assertFalse(old_claim.lock_path.exists())

    def test_failed_claim_publication_does_not_leave_lock(self):
        from localocr.job_registry import JobRegistry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.png"
            source.write_bytes(b"input")
            registry = JobRegistry(root / "jobs")
            request = registry.build_request(source, fake_profile(), root / "out")
            with patch.object(registry, "_write_manifest", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    registry.try_claim(request)
            self.assertEqual(list((root / "jobs").glob("*.lock")), [])

    def test_status_rejects_unbounded_path_and_non_object_manifest(self):
        from localocr.job_registry import JobRegistry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ("a" * 64 + ".json")).write_text("[]")
            registry = JobRegistry(root)
            self.assertFalse(registry.read_status("../secret")["ok"])
            self.assertFalse(registry.read_status("a" * 64)["ok"])

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

            auto = registry.build_request(
                source,
                fake_profile(),
                output_dir,
                request_variant="engine=auto;policy=smart-router-v3",
            )
            explicit = registry.build_request(
                source,
                fake_profile(),
                output_dir,
                request_variant="engine=ocr",
            )
            self.assertNotEqual(auto.job_key, explicit.job_key)

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
            self.assertIsNone(claim.lock_fd)

            active = registry.try_claim(request)

            self.assertEqual(active.kind, "active")
            self.assertEqual(active.response["status"], "active_localocr_task")
            self.assertEqual(active.response["recommendation"], "do_not_blindly_retry")
            self.assertEqual(active.response["job_key"], request.job_key)
            registry.release(claim)


if __name__ == "__main__":
    unittest.main()
