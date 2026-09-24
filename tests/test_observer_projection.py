from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


OBSERVED_AT = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
JOB_ID = "a1b2c3d4e5f60718"
OTHER_JOB_ID = "1029384756abcdef"


def write_manifest(job_dir: Path, job_id: str, **overrides: object) -> None:
    payload: dict[str, object] = {
        "schema_version": 1,
        "job_id": job_id,
        "job_key": f"{job_id}{'f' * 48}",
        "status": "running",
        "source_path": r"C:\private\medical-scan.png",
        "source_size": 1234,
        "source_sha256": "0" * 64,
        "engine": "ocr",
        "model_id": "ppocrv6-medium",
        "output_dir": r"C:\private\outputs",
        "started_at": "2026-07-25T11:59:30Z",
        "updated_at": "2026-07-25T11:59:55Z",
        "owner_pid": 4242,
        "command": ["python", "--secret"],
        "stdout": "PRIVATE_STDOUT",
        "stderr": "PRIVATE_STDERR",
        "broker_token": "PRIVATE_BROKER_TOKEN",
        "result": {
            "pages": [
                {
                    "blocks": [
                        {
                            "type": "text",
                            "text": "PRIVATE_OCR_TEXT",
                        }
                    ]
                }
            ]
        },
    }
    payload.update(overrides)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / f"{job_id}{'f' * 48}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class ObserverProjectionTest(unittest.TestCase):
    def test_real_registry_terminal_lifecycle_preserves_started_time(self) -> None:
        from localocr.job_registry import JobRegistry, JobRequest
        from localocr.observer import ObserverProjection

        for terminal_state in ("completed", "failed"):
            with self.subTest(terminal_state=terminal_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                job_dir = root / "jobs"
                job_id = "a1b2c3d4e5f60718" if terminal_state == "completed" else "1029384756abcdef"
                request = JobRequest(
                    job_key=f"{job_id}{'f' * 48}",
                    job_id=job_id,
                    source_path=root / "private-input.png",
                    source_size=1234,
                    source_sha256="0" * 64,
                    profile_id="ppocrv6-medium",
                    engine="ocr",
                    output_dir=root / "private-output",
                )
                registry = JobRegistry(job_dir)
                with patch(
                    "localocr.job_registry._now_iso",
                    side_effect=[
                        "2026-07-25T11:58:30Z",
                        "2026-07-25T11:58:31Z",
                        "2026-07-25T11:59:30Z",
                    ],
                ):
                    claim = registry.try_claim(request)
                    if terminal_state == "completed":
                        registry.complete(claim, {"output_files": {}})
                    else:
                        registry.fail(claim, RuntimeError("PRIVATE_FAILURE"))
                registry.release(claim)

                response = ObserverProjection(job_dir, now=lambda: OBSERVED_AT).get_job(job_id)

                self.assertIsNotNone(response)
                assert response is not None
                job = response["job"]
                self.assertEqual(terminal_state, job["state"])
                self.assertEqual("available", job["timing"]["status"])
                self.assertEqual("2026-07-25T11:58:30Z", job["timing"]["started_utc"])
                self.assertEqual("2026-07-25T11:59:30Z", job["timing"]["updated_utc"])
                self.assertEqual(60000, job["timing"]["elapsed_ms"])

    def test_oversized_manifest_is_skipped_without_reading_the_whole_file(self) -> None:
        from localocr.observer import OBSERVER_MANIFEST_LIMIT_BYTES, ObserverProjection

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(job_dir, JOB_ID)
            oversized = job_dir / f"{OTHER_JOB_ID}{'e' * 48}.json"
            oversized.write_text(
                json.dumps(
                    {
                        "job_id": OTHER_JOB_ID,
                        "status": "running",
                        "engine": "ocr",
                        "model_id": "ppocrv6-medium",
                        "started_at": "2026-07-25T11:59:30Z",
                        "updated_at": "2026-07-25T11:59:55Z",
                        "padding": "x" * OBSERVER_MANIFEST_LIMIT_BYTES,
                    }
                ),
                encoding="utf-8",
            )

            response = ObserverProjection(job_dir, now=lambda: OBSERVED_AT).list_jobs()

            self.assertEqual([JOB_ID], [job["job_id"] for job in response["jobs"]])

    def test_list_projects_only_stable_safe_fields(self) -> None:
        from localocr.observer import ObserverProjection

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(job_dir, JOB_ID)
            (job_dir / "corrupt.json").write_text("{", encoding="utf-8")

            response = ObserverProjection(job_dir, now=lambda: OBSERVED_AT).list_jobs()

            self.assertEqual(response["schema"], "local-ai-observer.jobs.v1")
            self.assertEqual(response["service"], "localocr")
            self.assertEqual(response["observed_utc"], "2026-07-25T12:00:00Z")
            self.assertEqual(len(response["jobs"]), 1)
            self.assertEqual(
                set(response["jobs"][0]),
                {"job_id", "state", "stage", "mode", "model", "progress", "timing", "tokens"},
            )
            self.assertEqual(
                response["jobs"][0],
                {
                    "job_id": JOB_ID,
                    "state": "running",
                    "stage": "processing",
                    "mode": "ocr",
                    "model": "ppocrv6-medium",
                    "progress": {
                        "status": "unavailable",
                        "completed": None,
                        "total": None,
                        "unit": None,
                    },
                    "timing": {
                        "status": "available",
                        "started_utc": "2026-07-25T11:59:30Z",
                        "updated_utc": "2026-07-25T11:59:55Z",
                        "elapsed_ms": 30000,
                    },
                    "tokens": {
                        "status": "not_applicable",
                        "input": None,
                        "output": None,
                        "tps": None,
                    },
                },
            )

            serialized = json.dumps(response, ensure_ascii=False)
            for private_value in (
                r"C:\private\medical-scan.png",
                r"C:\private\outputs",
                "4242",
                "--secret",
                "PRIVATE_STDOUT",
                "PRIVATE_STDERR",
                "PRIVATE_BROKER_TOKEN",
                "PRIVATE_OCR_TEXT",
                "source_sha256",
                "job_key",
            ):
                self.assertNotIn(private_value, serialized)

    def test_list_sorts_newest_first_and_limits_results(self) -> None:
        from localocr.observer import ObserverProjection

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(job_dir, JOB_ID, updated_at="2026-07-25T11:59:55Z")
            write_manifest(job_dir, OTHER_JOB_ID, updated_at="2026-07-25T11:59:56Z")

            response = ObserverProjection(job_dir, now=lambda: OBSERVED_AT).list_jobs(limit=1)

            self.assertEqual([job["job_id"] for job in response["jobs"]], [OTHER_JOB_ID])

    def test_detail_uses_distinct_schema_and_unknown_job_is_absent(self) -> None:
        from localocr.observer import ObserverProjection

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(
                job_dir,
                JOB_ID,
                status="completed",
                started_at="2026-07-25T11:58:30Z",
                updated_at="2026-07-25T11:59:30Z",
            )
            projection = ObserverProjection(job_dir, now=lambda: OBSERVED_AT)

            response = projection.get_job(JOB_ID)

            self.assertIsNotNone(response)
            assert response is not None
            self.assertEqual(response["schema"], "local-ai-observer.job.v1")
            self.assertEqual(response["service"], "localocr")
            self.assertEqual(response["job"]["state"], "completed")
            self.assertEqual(response["job"]["stage"], "completed")
            self.assertEqual(response["job"]["timing"]["elapsed_ms"], 60000)
            self.assertIsNone(projection.get_job("f" * 16))
            self.assertIsNone(projection.get_job("../private"))

    def test_untrusted_mode_and_model_are_not_projected(self) -> None:
        from localocr.observer import ObserverProjection

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(
                job_dir,
                JOB_ID,
                status=["running"],
                engine=r"C:\private\engine",
                model_id="model\nPRIVATE_OCR_TEXT",
                started_at="not-a-time",
                updated_at="also-not-a-time",
            )

            response = ObserverProjection(job_dir, now=lambda: OBSERVED_AT).list_jobs()

            job = response["jobs"][0]
            self.assertEqual(job["state"], "unknown")
            self.assertEqual(job["stage"], "unknown")
            self.assertIsNone(job["mode"])
            self.assertIsNone(job["model"])
            self.assertEqual(job["timing"]["status"], "unavailable")
            self.assertIsNone(job["timing"]["elapsed_ms"])
            self.assertNotIn("PRIVATE_OCR_TEXT", json.dumps(response))


@unittest.skipUnless(importlib.util.find_spec("fastapi") is not None, "FastAPI runtime is not installed")
class ObserverEndpointTest(unittest.TestCase):
    def test_http_routes_expose_only_safe_projection(self) -> None:
        from fastapi.testclient import TestClient

        from localocr.job_registry import JobRegistry
        from localocr import server

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp) / "jobs"
            write_manifest(job_dir, JOB_ID)
            previous_service = server._service
            server._service = SimpleNamespace(job_registry=JobRegistry(job_dir))
            try:
                client = TestClient(server.app, base_url="http://127.0.0.1:18665")

                list_response = client.get("/observer/jobs")
                detail_response = client.get(f"/observer/jobs/{JOB_ID}")
                missing_response = client.get("/observer/jobs/ffffffffffffffff")
                invalid_response = client.get("/observer/jobs/..%2Fprivate")
            finally:
                server._service = previous_service

            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(list_response.json()["schema"], "local-ai-observer.jobs.v1")
            self.assertEqual(detail_response.status_code, 200)
            self.assertEqual(detail_response.json()["schema"], "local-ai-observer.job.v1")
            self.assertEqual(missing_response.status_code, 404)
            self.assertEqual(missing_response.json(), {"detail": "observer_job_not_found"})
            self.assertEqual(invalid_response.status_code, 404)
            self.assertNotIn("private", invalid_response.text.casefold())


if __name__ == "__main__":
    unittest.main()
