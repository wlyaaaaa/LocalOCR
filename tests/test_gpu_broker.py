import subprocess
import runpy
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from localocr.gpu_broker import (
    GpuBrokerConflict,
    GpuBrokerError,
    GpuBrokerLease,
    GpuBrokerLeaseLost,
    verify_inherited_gpu_lease,
)
from localocr.service import OCRService
from localocr.model_registry import resolve_model_reference


class RecordingTransport:
    def __init__(self, acquire_ok=True):
        self.acquire_ok = acquire_ok
        self.calls = []
        self.owner = ""

    def __call__(self, action, payload):
        self.calls.append((action, dict(payload)))
        if action == "acquire":
            self.owner = payload["owner"]
            if not self.acquire_ok:
                return {
                    "ok": False,
                    "reason": "gpu_lease_active",
                    "owner": "chineseasr",
                }
            return {"ok": True, "token": "lease-token", "owner": payload["owner"]}
        return {"ok": True, "token": payload.get("token", ""), "owner": self.owner}


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
            with GpuBrokerLease(
                "localocr", transport=transport, renew_interval_seconds=0
            ):
                self.fail("work must not start")

        self.assertIn("chineseasr", str(raised.exception))
        self.assertEqual([call[0] for call in transport.calls], ["acquire"])

    def test_renewal_rejection_and_transport_failure_are_not_silent(self):
        for failure in ({"ok": False, "reason": "expired"},
                        {"ok": True, "owner": "another-owner"}, OSError("offline")):
            with self.subTest(failure=failure):
                base = RecordingTransport()

                def transport(action, payload):
                    if action == "renew":
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    return base(action, payload)

                with GpuBrokerLease("localocr", transport=transport,
                                    renew_interval_seconds=0.01) as lease:
                    lease._renew_thread.join(timeout=2)
                    self.assertFalse(lease._renew_thread.is_alive())
                    with self.assertRaises(GpuBrokerLeaseLost):
                        lease.raise_if_lost()

    def test_expired_lease_cannot_supply_worker_binding(self):
        with GpuBrokerLease("localocr", transport=RecordingTransport(),
                            renew_interval_seconds=0, ttl_seconds=1) as lease:
            with patch("localocr.gpu_broker.time.monotonic", return_value=time.monotonic() + 2):
                with self.assertRaises(GpuBrokerLeaseLost):
                    _ = lease.worker_binding

    def test_worker_requires_live_token_and_matching_owner(self):
        for binding in (None, {}, {"token": "t", "owner": "chineseasr"}):
            with self.subTest(binding=binding), self.assertRaises(GpuBrokerError):
                verify_inherited_gpu_lease(binding)
        binding = {"token": "t", "owner": "localocr", "base_url": "http://unused",
                   "ttl_seconds": 90}
        for response in ({"ok": False}, {"ok": True, "owner": "chineseasr"}):
            with patch("localocr.gpu_broker.default_transport", return_value=lambda *_: response):
                with self.assertRaises(GpuBrokerLeaseLost):
                    verify_inherited_gpu_lease(binding)
        with patch("localocr.gpu_broker.default_transport",
                   return_value=lambda *_: {"ok": True, "owner": "localocr"}):
            verify_inherited_gpu_lease(binding)

    def test_release_waits_for_renewal_and_cannot_be_followed_by_late_renew(self):
        renewing, allow_renew, released = threading.Event(), threading.Event(), threading.Event()
        base = RecordingTransport()

        def transport(action, payload):
            if action == "renew":
                renewing.set()
                if not allow_renew.wait(2):
                    raise TimeoutError("test renewal gate")
            return base(action, payload)

        lease = GpuBrokerLease("localocr", transport=transport, renew_interval_seconds=0.01)
        lease.__enter__()
        self.assertTrue(renewing.wait(2))

        def release():
            lease.__exit__(None, None, None)
            released.set()

        thread = threading.Thread(target=release)
        thread.start()
        try:
            self.assertFalse(released.wait(0.05))
        finally:
            allow_renew.set()
            thread.join(timeout=3)
        self.assertTrue(released.is_set())
        self.assertFalse(lease._renew_thread.is_alive())
        self.assertEqual([call[0] for call in base.calls], ["acquire", "renew", "release"])

    def test_release_failure_does_not_hide_original_execution_error(self):
        base = RecordingTransport()

        def transport(action, payload):
            if action == "release":
                raise GpuBrokerError("release failed")
            return base(action, payload)

        with self.assertRaisesRegex(ValueError, "original"):
            with GpuBrokerLease("localocr", transport=transport, renew_interval_seconds=0):
                raise ValueError("original")
        with self.assertRaisesRegex(GpuBrokerError, "release failed"):
            with GpuBrokerLease("localocr", transport=transport, renew_interval_seconds=0):
                pass


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
            profile = resolve_model_reference("ocr")
            route = SimpleNamespace(to_dict=lambda: {"effective_engine": "ocr"})

            def fake_process(*_args, **_kwargs):
                events.append("inference")
                return {
                    "pages": [
                        {
                            "blocks": [
                                {
                                    "type": "text",
                                    "text": "fake",
                                    "score": 0.99,
                                }
                            ]
                        }
                    ]
                }

            with patch(
                "localocr.service.select_model_profile_with_route",
                return_value=(profile, route),
            ):
                with patch.object(service, "process_file", side_effect=fake_process):
                    result = service.process_inputs([source], write_files=False)

        self.assertTrue(result["ok"])
        self.assertEqual(events, ["lease_enter", "inference", "lease_exit"])


class HeavyIntegrationGuardTests(unittest.TestCase):
    def test_run_tests_requires_explicit_heavy_opt_in_before_importing_models(self):
        root = Path(__file__).resolve().parent.parent
        for script in (root / "tests" / "run_tests.py", root / "scripts" / "download_models.py"):
            with self.subTest(script=script.name):
                completed = subprocess.run(
                    [sys.executable, "-X", "importtime", str(script)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("--allow-heavy", completed.stderr)
                self.assertNotIn("paddle", (completed.stdout + completed.stderr).lower())

    def test_model_warmup_uses_supervised_service_and_always_closes(self):
        root = Path(__file__).resolve().parent.parent
        main = runpy.run_path(str(root / "scripts" / "download_models.py"))["main"]
        service = Mock(gpu_summary="fake GPU")
        service.process_inputs.return_value = {"ok": True}
        with patch("localocr.service.OCRService", return_value=service):
            self.assertEqual(main(["--allow-heavy", "--timeout-sec", "17"]), 0)
        calls = service.process_inputs.call_args_list
        self.assertEqual([call.kwargs["engine_choice"] for call in calls], ["ocr", "vl", "structure"])
        self.assertTrue(all(call.kwargs["write_files"] is False for call in calls))
        self.assertTrue(all(call.kwargs["timeout_sec"] == 17 for call in calls))
        service.close.assert_called_once()

        service.reset_mock()
        service.process_inputs.side_effect = RuntimeError("warmup failed")
        with patch("localocr.service.OCRService", return_value=service):
            with self.assertRaisesRegex(RuntimeError, "warmup failed"):
                main(["--allow-heavy"])
        service.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
