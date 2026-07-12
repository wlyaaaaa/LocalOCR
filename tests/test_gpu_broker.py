import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from localocr.gpu_broker import GpuBrokerConflict, GpuBrokerLease
from localocr.service import OCRService


class RecordingTransport:
    def __init__(self, acquire_ok=True):
        self.acquire_ok = acquire_ok
        self.calls = []

    def __call__(self, action, payload):
        self.calls.append((action, dict(payload)))
        if action == "acquire":
            if not self.acquire_ok:
                return {"ok": False, "reason": "gpu_lease_active", "owner": "chineseasr"}
            return {"ok": True, "token": "lease-token", "owner": payload["owner"]}
        return {"ok": True, "token": payload.get("token", "")}


class GpuBrokerLeaseTests(unittest.TestCase):
    def test_context_acquires_and_releases(self):
        transport = RecordingTransport()

        with GpuBrokerLease("localocr", transport=transport, renew_interval_seconds=0):
            pass

        self.assertEqual([call[0] for call in transport.calls], ["acquire", "release"])
        self.assertEqual(transport.calls[-1][1]["token"], "lease-token")

    def test_conflict_raises_without_entering_work(self):
        transport = RecordingTransport(acquire_ok=False)

        with self.assertRaises(GpuBrokerConflict) as raised:
            with GpuBrokerLease("localocr", transport=transport, renew_interval_seconds=0):
                self.fail("work must not start")

        self.assertIn("chineseasr", str(raised.exception))
        self.assertEqual([call[0] for call in transport.calls], ["acquire"])


class OCRServiceLeaseTests(unittest.TestCase):
    def test_process_inputs_holds_lease_while_inference_runs(self):
        events = []

        class Lease:
            def __enter__(self):
                events.append("lease_enter")
                return self

            def __exit__(self, *_args):
                events.append("lease_exit")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "sample.png"
            source.write_bytes(b"not-an-image")
            service = OCRService(
                probe_on_start=False,
                tmp_dir=Path(tmp) / "pages",
                job_dir=Path(tmp) / "jobs",
                gpu_lease_factory=lambda owner: Lease(),
            )
            profile = SimpleNamespace(engine="ocr", id="ppocrv6-medium")
            route = SimpleNamespace(to_dict=lambda: {"effective_engine": "ocr"})

            def fake_process(*_args, **_kwargs):
                events.append("inference")
                return {"pages": []}

            with patch("localocr.service.select_model_profile_with_route", return_value=(profile, route)):
                with patch.object(service, "process_file", side_effect=fake_process):
                    result = service.process_inputs([source], write_files=False)

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["lease_enter", "inference", "lease_exit"])


class HeavyIntegrationGuardTests(unittest.TestCase):
    def test_run_tests_requires_explicit_heavy_opt_in_before_importing_models(self):
        script = Path(__file__).resolve().parent / "run_tests.py"

        completed = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("--allow-heavy", completed.stderr)


if __name__ == "__main__":
    unittest.main()
