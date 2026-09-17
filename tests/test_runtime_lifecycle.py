"""Process-level lifecycle contracts for the supervised LocalOCR runtime.

These tests deliberately use only standard-library fake predictors.  They do
not import Paddle, start the production API, or read OCR input content.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import psutil


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from localocr.runtime import ExecutionError, InferenceRuntime, _process_identity  # noqa: E402
from localocr.service import OCRService  # noqa: E402


MiB = 1024 * 1024


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _write_pid(path_value: str | None, pid: int) -> None:
    if path_value:
        _atomic_write(Path(path_value), str(pid))


def _fake_result(payload: dict[str, Any], cache: dict[str, Any]) -> dict[str, Any]:
    return {
        "engine": "lifecycle-fake",
        "model": payload["profile_id"],
        "device": payload["device"],
        "pages": [
            {
                "page_index": 0,
                "blocks": [{"type": "text", "text": "fake", "score": 1.0}],
            }
        ],
        "worker_pid": os.getpid(),
        "model_loads": cache["model_loads"],
    }


def lifecycle_predictor(payload: dict[str, Any], cache: dict[str, Any], emit: Callable[[dict], None]) -> dict[str, Any]:
    """Pickle-safe worker fake that can leave a normal child behind on purpose."""

    profile_id = str(payload["profile_id"])
    if cache.get("profile_id") != profile_id:
        cache.clear()
        cache["profile_id"] = profile_id
        cache["model_loads"] = 1

    emit({"stage": "recognizing", "loaded_model": profile_id})
    mode = str(payload.get("test_mode") or "success")
    if mode == "success":
        return _fake_result(payload, cache)

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _write_pid(payload.get("child_pid_file"), child.pid)
    emit({"stage": "recognizing", "child_pid": child.pid})

    if mode == "crash":
        # Simulate an uncatchable worker death.  The runtime guard must reap
        # this ordinary child too, not merely the multiprocessing worker.
        os._exit(37)
    if mode == "allocate":
        cache["allocation"] = bytearray(int(payload["allocation_bytes"]))
    if mode not in {"hang", "allocate"}:
        raise AssertionError(f"unknown lifecycle fake mode: {mode}")
    while True:
        time.sleep(0.05)


def service_predictor(payload: dict[str, Any], cache: dict[str, Any], emit: Callable[[dict], None]) -> dict[str, Any]:
    """Derive a test mode from the immutable snapshot filename used by service."""

    request = dict(payload)
    if Path(str(request["path"])).name.startswith("hang"):
        request["test_mode"] = "hang"
    return lifecycle_predictor(request, cache, emit)


def _coordinator_for_hard_death(state_path: str, child_pid_path: str) -> None:
    """Stand in for an API process that is subsequently SIGKILLed by its parent."""

    runtime = InferenceRuntime(predictor=lifecycle_predictor)

    def progress(event: dict[str, Any]) -> None:
        worker_pid = event.get("worker_pid")
        if isinstance(worker_pid, int):
            _atomic_write(Path(state_path), json.dumps({"worker_pid": worker_pid}))

    runtime.predict(
        {
            "path": "/tmp/hard-death.png",
            "profile_id": "profile-parent-death",
            "device": "cpu",
            "tmp_dir": "/tmp",
            "test_mode": "hang",
            "child_pid_file": child_pid_path,
        },
        deadline=time.monotonic() + 60,
        cancel=threading.Event(),
        check_lease=lambda: None,
        progress=progress,
    )


class FakeLease:
    def __init__(self, lost: threading.Event) -> None:
        self.lost = lost
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeLease":
        self.entered = True
        return self

    def __exit__(self, *_args: object) -> None:
        self.exited = True

    @property
    def worker_binding(self) -> None:
        return None

    def raise_if_lost(self) -> None:
        if self.lost.is_set():
            raise ExecutionError("gpu_lease_lost", "test GPU lease was lost")


def _pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 6.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError(message)


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kill_test_process_group(pid: int | None) -> None:
    if not isinstance(pid, int) or pid <= 0:
        return
    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _quiet_close(runtime: InferenceRuntime) -> None:
    try:
        runtime.close()
    except Exception:
        # Explicit assertions report a lifecycle cleanup failure.  Cleanup must
        # still avoid stranding a later test if an assertion already failed.
        pass


class RuntimeLifecycleTest(unittest.TestCase):
    def _runtime(self, *, memory_limit_bytes: int = 30_000_000_000) -> InferenceRuntime:
        runtime = InferenceRuntime(
            predictor=lifecycle_predictor,
            memory_limit_bytes=memory_limit_bytes,
        )
        self.addCleanup(_quiet_close, runtime)
        return runtime

    @staticmethod
    def _payload(root: Path, *, profile_id: str, mode: str = "success", child_pid_path: Path | None = None, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": str(root / "synthetic.png"),
            "profile_id": profile_id,
            "device": "cpu",
            "tmp_dir": str(root / "pages"),
            "probe_gpu": False,
            "test_mode": mode,
        }
        if child_pid_path is not None:
            payload["child_pid_file"] = str(child_pid_path)
        payload.update(extra)
        return payload

    @staticmethod
    def _predict(
        runtime: InferenceRuntime,
        payload: dict[str, Any],
        *,
        timeout: float = 6.0,
        cancel: threading.Event | None = None,
        check_lease: Callable[[], None] | None = None,
        progress: Callable[[dict], None] | None = None,
    ) -> dict[str, Any]:
        return runtime.predict(
            payload,
            deadline=time.monotonic() + timeout,
            cancel=cancel or threading.Event(),
            check_lease=check_lease or (lambda: None),
            progress=progress or (lambda _event: None),
        )

    def _start_hanging_request(
        self,
        runtime: InferenceRuntime,
        payload: dict[str, Any],
        *,
        timeout: float,
        check_lease: Callable[[], None] | None = None,
    ) -> tuple[threading.Thread, threading.Event, dict[str, Any]]:
        cancel = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["result"] = self._predict(
                    runtime,
                    payload,
                    timeout=timeout,
                    cancel=cancel,
                    check_lease=check_lease,
                )
            except BaseException as exc:  # Assertions inspect the exact error below.
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, cancel, outcome

    def _assert_worker_tree_stopped(self, worker_pid: int | None, child_pid: int | None) -> None:
        _wait_until(
            lambda: not _pid_alive(worker_pid) and not _pid_alive(child_pid),
            message=f"worker tree survived: worker={worker_pid}, child={child_pid}",
        )

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux stable process identity")
    def test_linux_process_identity_ignores_wall_clock_and_detects_pid_reuse(self) -> None:
        before = _process_identity(os.getpid())
        self.assertIsNotNone(before)
        with patch("localocr.runtime.psutil.Process", side_effect=AssertionError("wall-clock API must not be used")), patch("localocr.runtime.time.time", return_value=1):
            self.assertEqual(_process_identity(os.getpid()), before)
        fields = ["S"] + ["0"] * 18 + ["12345"]
        with patch("localocr.runtime.Path.read_text", return_value="777 (name ) with spaces) " + " ".join(fields)):
            self.assertEqual(_process_identity(777), (777, 12345))
        fields[19] = "12346"
        with patch("localocr.runtime.Path.read_text", return_value="777 (reused) " + " ".join(fields)):
            self.assertEqual(_process_identity(777), (777, 12346))
        fields[0] = "Z"
        with patch("localocr.runtime.Path.read_text", return_value="777 (zombie) " + " ".join(fields)):
            self.assertIsNone(_process_identity(777))

    def test_short_lived_calling_threads_do_not_kill_warm_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = self._runtime(); root = Path(tmp); outcomes = []
            def call():
                try:
                    outcomes.append(self._predict(runtime, self._payload(root, profile_id="thread-warm")))
                except Exception as exc:
                    outcomes.append(exc)
            worker = None
            for _ in range(12):
                thread = threading.Thread(target=call)
                thread.start(); thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(outcomes[-1], dict)
                worker = worker or outcomes[-1]["worker_pid"]
                time.sleep(0.1)
                self.assertEqual(runtime.pid, worker, "creator-thread retirement must not kill the process")
                self.assertEqual(outcomes[-1]["worker_pid"], worker)

    def test_same_model_is_warm_and_switch_replaces_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime()

            first = self._predict(runtime, self._payload(root, profile_id="profile-a"))
            first_pid = first["worker_pid"]
            second = self._predict(runtime, self._payload(root, profile_id="profile-a"))
            switched = self._predict(runtime, self._payload(root, profile_id="profile-b"))

            self.assertEqual(first_pid, second["worker_pid"])
            self.assertEqual(first["model_loads"], 1)
            self.assertEqual(second["model_loads"], 1)
            self.assertNotEqual(first_pid, switched["worker_pid"])
            self.assertEqual(switched["model_loads"], 1)
            self.assertEqual(runtime.profile_id, "profile-b")
            self.assertFalse(_pid_alive(first_pid), "profile switch left the old warm worker alive")

    def test_same_model_revision_change_replaces_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime()
            first = self._predict(runtime, self._payload(root, profile_id="profile-a", profile_revision="old"))
            second = self._predict(runtime, self._payload(root, profile_id="profile-a", profile_revision="new"))
            self.assertNotEqual(first["worker_pid"], second["worker_pid"])
            self.assertFalse(_pid_alive(first["worker_pid"]))

    def test_timeout_kills_full_worker_tree_and_next_request_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "timeout-child.pid"
            runtime = self._runtime()
            thread, _cancel, outcome = self._start_hanging_request(
                runtime,
                self._payload(root, profile_id="profile-timeout", mode="hang", child_pid_path=child_path),
                timeout=2.0,
            )
            _wait_until(lambda: _read_pid(child_path) is not None, message="timeout fake child did not start")
            worker_pid = runtime.pid
            child_pid = _read_pid(child_path)
            thread.join(5)

            self.assertFalse(thread.is_alive(), "timeout did not return")
            error = outcome.get("error")
            self.assertIsInstance(error, ExecutionError)
            self.assertEqual(error.code, "execution_timeout")
            self._assert_worker_tree_stopped(worker_pid, child_pid)
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-timeout"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    def test_cancel_kills_full_worker_tree_and_next_request_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "cancel-child.pid"
            runtime = self._runtime()
            thread, cancel, outcome = self._start_hanging_request(
                runtime,
                self._payload(root, profile_id="profile-cancel", mode="hang", child_pid_path=child_path),
                timeout=10.0,
            )
            _wait_until(lambda: _read_pid(child_path) is not None, message="cancel fake child did not start")
            worker_pid = runtime.pid
            child_pid = _read_pid(child_path)
            cancel.set()
            thread.join(5)

            self.assertFalse(thread.is_alive(), "cancel did not return")
            error = outcome.get("error")
            self.assertIsInstance(error, ExecutionError)
            self.assertEqual(error.code, "execution_cancelled")
            self._assert_worker_tree_stopped(worker_pid, child_pid)
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-cancel"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    def test_lease_loss_kills_full_worker_tree_and_next_request_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "lease-child.pid"
            runtime = self._runtime()
            lease_lost = threading.Event()

            def check_lease() -> None:
                if lease_lost.is_set():
                    raise ExecutionError("gpu_lease_lost", "test lease lost")

            thread, _cancel, outcome = self._start_hanging_request(
                runtime,
                self._payload(root, profile_id="profile-lease", mode="hang", child_pid_path=child_path),
                timeout=10.0,
                check_lease=check_lease,
            )
            _wait_until(lambda: _read_pid(child_path) is not None, message="lease-loss fake child did not start")
            worker_pid = runtime.pid
            child_pid = _read_pid(child_path)
            lease_lost.set()
            thread.join(5)

            self.assertFalse(thread.is_alive(), "lease loss did not return")
            error = outcome.get("error")
            self.assertIsInstance(error, ExecutionError)
            self.assertEqual(error.code, "gpu_lease_lost")
            self._assert_worker_tree_stopped(worker_pid, child_pid)
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-lease"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    def test_worker_crash_reaps_descendant_and_next_request_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "crash-child.pid"
            runtime = self._runtime()
            with self.assertRaises(ExecutionError) as raised:
                self._predict(
                    runtime,
                    self._payload(root, profile_id="profile-crash", mode="crash", child_pid_path=child_path),
                    timeout=5.0,
                )

            self.assertEqual(raised.exception.code, "worker_exited")
            if "worker_exit_code" in raised.exception.context:
                self.assertEqual(raised.exception.context["worker_exit_code"], 37)
            child_pid = _read_pid(child_path)
            self.assertIsNotNone(child_pid, "crashing fake worker did not create its ordinary child")
            self.assertIsNone(runtime.pid)
            self._assert_worker_tree_stopped(None, child_pid)
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-crash"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    def test_scaled_rss_limit_kills_worker_without_allocating_30gb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = self._runtime()
            # Establish a real warm-worker baseline first, then shrink the
            # production 30 GB guard to a small, deterministic test threshold.
            self._predict(runtime, self._payload(root, profile_id="profile-rss"))
            baseline = runtime._memory_bytes()
            runtime.memory_limit_bytes = baseline + 16 * MiB

            with self.assertRaises(ExecutionError) as raised:
                self._predict(
                    runtime,
                    self._payload(
                        root,
                        profile_id="profile-rss",
                        mode="allocate",
                        allocation_bytes=64 * MiB,
                    ),
                    timeout=8.0,
                )

            self.assertEqual(raised.exception.code, "memory_limit_exceeded")
            self.assertGreater(runtime.peak_memory_bytes, runtime.memory_limit_bytes)
            self.assertIsNone(runtime.pid)
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-rss"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    def test_second_concurrent_pipe_request_is_rejected_without_corrupting_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_path = root / "pipe-child.pid"
            runtime = self._runtime()
            thread, cancel, outcome = self._start_hanging_request(
                runtime,
                self._payload(root, profile_id="profile-pipe", mode="hang", child_pid_path=child_path),
                timeout=10.0,
            )
            _wait_until(lambda: _read_pid(child_path) is not None, message="first pipe request did not start")

            with self.assertRaises(ExecutionError) as raised:
                self._predict(runtime, self._payload(root, profile_id="profile-pipe"))

            self.assertEqual(raised.exception.code, "localocr_busy")
            cancel.set()
            thread.join(5)
            self.assertIsInstance(outcome.get("error"), ExecutionError)
            self.assertEqual(outcome["error"].code, "execution_cancelled")
            recovered = self._predict(runtime, self._payload(root, profile_id="profile-pipe"))
            self.assertTrue(_pid_alive(recovered["worker_pid"]))

    @unittest.skipUnless(os.name == "posix", "parent-death process-group semantics are verified in WSL/Linux")
    def test_hard_killed_api_parent_reaps_worker_and_ordinary_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_path = root / "coordinator-state.json"
            child_path = root / "parent-death-child.pid"
            coordinator = multiprocessing.get_context("spawn").Process(
                target=_coordinator_for_hard_death,
                args=(str(state_path), str(child_path)),
                name="localocr-runtime-parent-death-test",
            )
            worker_pid: int | None = None
            child_pid: int | None = None
            try:
                coordinator.start()

                def worker_started() -> bool:
                    nonlocal worker_pid
                    try:
                        worker_pid = int(json.loads(state_path.read_text(encoding="utf-8"))["worker_pid"])
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        return False
                    return _pid_alive(worker_pid)

                _wait_until(worker_started, timeout=10.0, message="test coordinator did not start a worker")
                _wait_until(lambda: _read_pid(child_path) is not None, timeout=10.0, message="worker did not start ordinary child")
                child_pid = _read_pid(child_path)
                self.assertTrue(_pid_alive(child_pid))

                os.kill(coordinator.pid, signal.SIGKILL)
                coordinator.join(5)
                self.assertFalse(coordinator.is_alive(), "test API coordinator ignored SIGKILL")
                self._assert_worker_tree_stopped(worker_pid, child_pid)
            finally:
                if coordinator.is_alive():
                    coordinator.kill()
                    coordinator.join(5)
                _kill_test_process_group(worker_pid)
                _kill_test_process_group(child_pid)

    def test_service_rejects_different_busy_request_without_writing_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hang_source = root / "hang.png"
            other_source = root / "other.png"
            hang_source.write_bytes(b"not-an-ocr-input")
            other_source.write_bytes(b"not-an-ocr-input")
            lost = threading.Event()
            leases: list[FakeLease] = []

            def lease_factory(_owner: str) -> FakeLease:
                lease = FakeLease(lost)
                leases.append(lease)
                return lease

            runtime = InferenceRuntime(predictor=service_predictor)
            service = OCRService(
                device="cpu",
                tmp_dir=root / "tmp",
                job_dir=root / "jobs",
                probe_on_start=False,
                gpu_lease_factory=lease_factory,
                runtime=runtime,
            )
            self.addCleanup(service.close)
            outcome: dict[str, Any] = {}

            def run_first() -> None:
                try:
                    outcome["result"] = service.process_inputs(
                        [hang_source], engine_choice="ocr", write_files=False, timeout_sec=10.0
                    )
                except BaseException as exc:
                    outcome["error"] = exc

            thread = threading.Thread(target=run_first, daemon=True)
            thread.start()
            _wait_until(lambda: bool(service.active_jobs), message="service did not mark first request active")
            active = service.active_jobs[0]
            busy = service.process_inputs([other_source], engine_choice="ocr", write_files=False, timeout_sec=10.0)

            self.assertFalse(busy["ok"])
            self.assertEqual(busy["error_code"], "localocr_busy")
            self.assertEqual(busy["active_jobs"][0]["job_key"], active["job_key"])
            self.assertEqual(list((root / "jobs").glob("*")) if (root / "jobs").exists() else [], [])

            self.assertTrue(service.cancel(active["job_key"]))
            thread.join(5)
            self.assertFalse(thread.is_alive(), "service cancellation did not return")
            self.assertIsInstance(outcome.get("error"), ExecutionError)
            self.assertEqual(outcome["error"].code, "execution_cancelled")
            self.assertTrue(leases and leases[0].entered and leases[0].exited)

    def test_service_lease_loss_stops_worker_and_allows_next_cpu_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hang_source = root / "hang.png"
            success_source = root / "success.png"
            hang_source.write_bytes(b"not-an-ocr-input")
            success_source.write_bytes(b"not-an-ocr-input")
            lost = threading.Event()
            leases: list[FakeLease] = []

            def lease_factory(_owner: str) -> FakeLease:
                lease = FakeLease(lost)
                leases.append(lease)
                return lease

            runtime = InferenceRuntime(predictor=service_predictor)
            service = OCRService(
                device="cpu",
                tmp_dir=root / "tmp",
                job_dir=root / "jobs",
                probe_on_start=False,
                gpu_lease_factory=lease_factory,
                runtime=runtime,
            )
            self.addCleanup(service.close)
            outcome: dict[str, Any] = {}

            def run_first() -> None:
                try:
                    service.process_inputs([hang_source], engine_choice="ocr", write_files=False, timeout_sec=10.0)
                except BaseException as exc:
                    outcome["error"] = exc

            thread = threading.Thread(target=run_first, daemon=True)
            thread.start()
            _wait_until(lambda: bool(service.active_jobs), message="service did not start lease-loss request")
            lost.set()
            thread.join(5)

            self.assertFalse(thread.is_alive(), "service lease loss did not return")
            self.assertIsInstance(outcome.get("error"), ExecutionError)
            self.assertEqual(outcome["error"].code, "gpu_lease_lost")
            self.assertTrue(leases and leases[0].entered and leases[0].exited)

            # The next request receives a fresh lease after the broker recovers.
            lost.clear()
            response = service.process_inputs(
                [success_source], engine_choice="ocr", write_files=False, timeout_sec=5.0
            )
            self.assertTrue(response["ok"])
            self.assertEqual(response["results"][0]["engine"], "lifecycle-fake")
            self.assertEqual(service.loaded_models, ["ppocrv6-medium"])


if __name__ == "__main__":
    unittest.main()
