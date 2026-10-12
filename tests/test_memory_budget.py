import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from localocr.memory_budget import host_memory_bytes
from localocr.runtime import ExecutionError, InferenceRuntime

class MemoryBudgetTests(unittest.TestCase):
    def test_wsl_host_fields_are_kib_and_bridge_is_bounded(self):
        with patch("localocr.memory_budget.sys.platform", "linux"), patch("localocr.memory_budget.platform.release", return_value="microsoft-standard-WSL2"), patch("localocr.memory_budget.subprocess.run") as run:
            run.return_value = SimpleNamespace(stdout='{"total":64000000,"available":8000000}')
            self.assertEqual(host_memory_bytes(), (64000000 * 1024, 8000000 * 1024))
            self.assertEqual((run.call_args.args[0][0], run.call_args.args[0][3]), ("powershell.exe", "-NonInteractive"))
            self.assertEqual(run.call_args.kwargs["timeout"], 8)
    def test_request_budget_rejects_before_model_or_caps_existing_supervision(self):
        gib = 1024**3
        for sample, configured, code, effective in (
            (OSError("host unavailable"), 30 * gib, "memory_status_unavailable", None),
            ((20 * gib, 2 * gib), 30 * gib, "memory_pressure", None),
            ((20 * gib, 5 * gib), 30 * gib, "test_stop", 4 * gib),
            ((20 * gib, 5 * gib), 3 * gib, "test_stop", 3 * gib),
            ("cancel", 30 * gib, "execution_cancelled", None),
            ("timeout", 30 * gib, "execution_timeout", None),
        ):
            with self.subTest(sample=sample, configured=configured):
                runtime = InferenceRuntime(memory_limit_bytes=configured)
                cancel = threading.Event()
                provider = {"side_effect": sample} if isinstance(sample, Exception) else {"return_value": sample}
                if sample == "cancel": provider = {"side_effect": lambda **_: cancel.set() or (20 * gib, 5 * gib)}
                if sample == "timeout": provider = {"side_effect": lambda **_: time.sleep(0.06) or (20 * gib, 5 * gib)}
                with patch("localocr.runtime.host_memory_bytes", **provider) as probe, patch.object(
                    runtime, "_memory_bytes", return_value=gib
                ), patch.object(runtime, "_start", side_effect=ExecutionError("test_stop", "")) as start, patch.object(runtime, "close") as close:
                    with self.assertRaises(ExecutionError) as raised:
                        runtime.predict({"profile_id": "fake"}, deadline=time.monotonic() + (0.05 if sample == "timeout" else 10), cancel=cancel, check_lease=lambda: None, progress=lambda _: None)
                    self.assertEqual(raised.exception.code, code)
                    self.assertEqual(start.call_count, int(effective is not None))
                    close.assert_called_once()
                    probe.assert_called_once()
                    if effective is not None:
                        self.assertEqual(runtime.effective_memory_limit_bytes, effective)
