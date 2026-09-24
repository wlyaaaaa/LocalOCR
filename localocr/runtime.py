"""One supervised, warm inference process; no persistent work queue or service."""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

import psutil


DEFAULT_TIMEOUT_SEC = 300.0
MAX_TIMEOUT_SEC = 7200.0
DEFAULT_MEMORY_LIMIT_BYTES = 30_000_000_000


class ExecutionError(RuntimeError):
    def __init__(self, code: str, detail: str, *, http_status: int = 500) -> None:
        super().__init__(detail)
        self.code = code
        self.http_status = http_status
        self.context: dict[str, Any] = {}


def _process_identity(pid: int) -> tuple[int, int | float] | None:
    if sys.platform.startswith("linux"):
        # /proc start ticks are stable across NTP/WSL wall-clock corrections.
        # Keep PID-reuse protection without using boot-time-derived epoch time.
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = raw.rsplit(")", 1)[1].split()
            if fields[0] in {"Z", "X", "x"}:
                return None
            return pid, int(fields[19])  # Linux /proc stat field 22.
        except (OSError, ValueError, IndexError):
            return None
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return pid, process.create_time()
    except psutil.Error:
        return None


def _guard_process_group(parent_identity: tuple[int, int | float], worker_pid: int) -> None:
    """A tiny POSIX guard also removes descendants after an uncatchable parent exit."""
    while True:
        if (
            os.getppid() != worker_pid
            or _process_identity(parent_identity[0]) != parent_identity
        ):
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os._exit(0)
        time.sleep(0.2)


def _predict(
    payload: dict[str, Any], cache: dict, emit: Callable[[dict], None]
) -> dict:
    # The coordinator never imports Paddle. Verify the parent's live lease
    # before a probe, model import, model load, or inference in this process.
    device = payload["device"]
    uses_gpu = device.lower().startswith(("gpu", "cuda"))
    from .model_registry import ModelProfile, get_engine, resolve_model_reference

    profile = ModelProfile(**payload["model_profile"]) if payload.get("model_profile") else resolve_model_reference(payload["profile_id"])
    if uses_gpu:
        from .gpu_broker import verify_inherited_gpu_lease

        verify_inherited_gpu_lease(payload.get("lease"))
        module_loading = profile.options.get("cuda_module_loading")
        if module_loading:
            # Set before even the GPU probe imports Paddle. The profile and its
            # cache/sidecar hash own this workaround, not the machine environment.
            os.environ["CUDA_MODULE_LOADING"] = module_loading
    if uses_gpu and payload.get("probe_gpu") and "gpu" not in cache:
        from .gpu_probe import format_probe, probe_gpu

        if profile.runtime_backend == "torch":
            from .gpu_probe import probe_torch_gpu
            info = probe_torch_gpu(device=device)
        else:
            info = probe_gpu() if device in {"gpu:0", "cuda:0"} else probe_gpu(device=device)
        cache["gpu"] = format_probe(info)
    emit({"stage": "loading_model", "gpu": cache.get("gpu")})

    from .pdf_utils import input_page_count, iter_input_pages, uniform_page_hint

    if "engine" not in cache:
        cache["engine"] = get_engine(profile, device=device)
    engine = cache["engine"]
    ensure = getattr(engine, "_ensure", None)
    if callable(ensure):
        ensure()
    emit({"stage": "recognizing", "loaded_model": profile.id, "gpu": cache.get("gpu")})
    source = Path(payload["path"])
    expected = input_page_count(source)
    selected = payload.get("page_indices")
    pages = []
    result = {"engine": engine.engine_name, "model": engine.model_name,
              "device": device, "expected_page_count": expected, "pages": pages}
    from .checkpoints import save_header, save_page
    checkpoint_dir = Path(payload["checkpoint_dir"]) if payload.get("checkpoint_dir") else None
    if checkpoint_dir is not None:
        save_header(checkpoint_dir, {**result, "model_id": profile.id, "engine_key": profile.engine}, payload)
    try:
        for index, image_path, metadata in iter_input_pages(
            source, Path(payload["tmp_dir"]), indices=selected,
            dpi=float(profile.preprocessing.get("pdf_dpi", 216)),
            max_pixels=int(profile.preprocessing.get("max_render_pixels", 24_000_000)),
        ):
            emit({"stage": "recognizing", "completed_pages": len(pages), "total_pages": expected})
            page_result = engine.predict_image(str(image_path))
            returned = page_result.get("pages") or []
            if len(returned) != 1:
                raise ExecutionError("page_coverage_mismatch", f"Input page {index} produced {len(returned)} result pages")
            page = returned[0]
            page["page_index"] = index
            page["model_id"] = profile.id
            page["engine_key"] = profile.engine
            page["page_angle"] = page.get("page_angle", page_result.get("page_angle"))
            page.update(metadata)
            page["routing_uniform_hint"] = uniform_page_hint(image_path)
            if profile.engine == "ocr":
                from .difficulty import page_structure_signals
                page["structure_signals"] = page_structure_signals(page, image_path)
            pages.append(page)
            if checkpoint_dir is not None:
                save_page(checkpoint_dir, page)
    except BaseException as exc:
        if pages:
            error = exc if isinstance(exc, ExecutionError) else ExecutionError("inference_failed", f"{type(exc).__name__}: {exc}")
            error.context["partial_result"] = {**result, "pages": list(pages), "model_id": profile.id,
                                                "engine_key": profile.engine, "source_file": str(source)}
            if error is exc:
                raise
            raise error from exc
        raise
    result["requested_page_indices"] = list(range(expected)) if selected is None else list(selected)
    emit({"stage": "recognized", "completed_pages": len(pages), "total_pages": expected})
    result.update(
        source_file=str(source), engine_key=profile.engine, model_id=profile.id
    )
    if uses_gpu:
        result["cuda_module_loading"] = os.environ.get("CUDA_MODULE_LOADING", "default")
    return result


def _worker_main(
    connection: Connection, parent_identity: tuple[int, int | float], predictor=None
) -> None:
    guard_pid = None
    if os.name == "posix":
        os.setsid()
        worker_pid = os.getpid()
        # Guard the coordinator process, not the transient thread that spawned us.
        # PR_SET_PDEATHSIG follows that thread and incorrectly kills warm workers
        # when API executor threads retire. The guard also reaps ordinary children.
        guard_pid = os.fork()
        if guard_pid == 0:
            connection.close()
            _guard_process_group(parent_identity, worker_pid)
    if _process_identity(parent_identity[0]) != parent_identity:
        os._exit(1)
    cache: dict[str, Any] = {}
    runner = predictor or _predict
    try:
        while True:
            payload = connection.recv()
            if payload is None:
                return
            request_id = payload["request_id"]

            def emit(event: dict, request_id=request_id) -> None:
                connection.send({"type": "progress", "request_id": request_id, **event})

            try:
                result = runner(payload, cache, emit)
                connection.send(
                    {"type": "result", "request_id": request_id, "result": result}
                )
            except BaseException as exc:
                connection.send(
                    {
                        "type": "error",
                        "request_id": request_id,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "traceback_tail": traceback.format_exc()[-2000:],
                        "code": getattr(exc, "code", "inference_failed"),
                        "http_status": getattr(exc, "http_status", 500),
                        "context": getattr(exc, "context", {}),
                    }
                )
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()
        if guard_pid is not None:
            try:
                os.kill(guard_pid, signal.SIGKILL)
                os.waitpid(guard_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


class InferenceRuntime:
    """Single in-flight request; reuse the same model, replace it on model switch."""

    def __init__(
        self, *, predictor=None, memory_limit_bytes: int = DEFAULT_MEMORY_LIMIT_BYTES
    ) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._predictor = predictor
        self.memory_limit_bytes = memory_limit_bytes
        self._state_lock = threading.RLock()
        self._call_lock = threading.Lock()
        self._process = None
        self._connection: Connection | None = None
        self.profile_id: str | None = None
        self.profile_revision: str | None = None
        self.loaded = False
        self.generation: str | None = None
        self.peak_memory_bytes = 0
        self.gpu_summary: str | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def _start(self, profile_id: str, revision: str | None = None) -> None:
        with self._state_lock:
            if self.pid is not None and self.profile_id == profile_id and self.profile_revision == revision:
                return
            self.close()
            connection, child_connection = self._context.Pipe()
            parent_identity = _process_identity(os.getpid())
            if parent_identity is None:
                raise ExecutionError(
                    "worker_start_failed", "Coordinator identity is unavailable."
                )
            process = self._context.Process(
                target=_worker_main,
                args=(child_connection, parent_identity, self._predictor),
                name="localocr-inference",
            )
            process.start()
            child_connection.close()
            self._process = process
            self._connection = connection
            self.profile_id = profile_id
            self.profile_revision = revision
            self.generation = uuid.uuid4().hex
            self.loaded = False

    def _memory_bytes(self) -> int:
        process = psutil.Process(os.getpid())
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                pass
        self.peak_memory_bytes = max(self.peak_memory_bytes, total)
        return total

    def predict(
        self,
        payload: dict[str, Any],
        *,
        deadline: float,
        cancel: threading.Event,
        check_lease: Callable[[], None],
        progress: Callable[[dict], None],
    ) -> dict:
        if not self._call_lock.acquire(blocking=False):
            raise ExecutionError(
                "localocr_busy", "Inference is already running.", http_status=409
            )
        try:
            check_lease()
            self._start(payload["profile_id"], payload.get("profile_revision"))
            connection = self._connection
            process = self._process
            assert connection is not None and process is not None
            request_id = uuid.uuid4().hex
            connection.send({**payload, "request_id": request_id})
            progress(
                {
                    "stage": "starting_worker",
                    "worker_pid": process.pid,
                    "worker_generation": self.generation,
                }
            )
            heartbeat = time.monotonic()
            while True:
                if cancel.is_set():
                    raise ExecutionError(
                        "execution_cancelled",
                        "The OCR task was cancelled.",
                        http_status=409,
                    )
                if time.monotonic() >= deadline:
                    raise ExecutionError(
                        "execution_timeout",
                        "The OCR execution deadline was exceeded.",
                        http_status=504,
                    )
                check_lease()
                if self._memory_bytes() > self.memory_limit_bytes:
                    raise ExecutionError(
                        "memory_limit_exceeded",
                        "The OCR task exceeded its memory limit.",
                        http_status=503,
                    )
                if time.monotonic() - heartbeat >= 5:
                    progress(
                        {
                            "worker_pid": process.pid,
                            "worker_generation": self.generation,
                        }
                    )
                    heartbeat = time.monotonic()
                if connection.poll(0.1):
                    message = connection.recv()
                    if message.get("request_id") != request_id:
                        raise ExecutionError(
                            "worker_protocol_error",
                            "Unexpected inference response identity.",
                        )
                    if message["type"] == "progress":
                        if message.get("loaded_model"):
                            self.loaded = True
                        if message.get("gpu"):
                            self.gpu_summary = message["gpu"]
                        progress(message)
                    elif message["type"] == "result":
                        check_lease()
                        return message["result"]
                    elif message["type"] == "error":
                        error = ExecutionError(message.get("code", "inference_failed"), message["detail"], http_status=message.get("http_status", 500))
                        error.context.update(message.get("context") or {})
                        raise error
                    else:
                        raise ExecutionError(
                            "worker_protocol_error", "Unknown inference response type."
                        )
                elif not process.is_alive():
                    raise ExecutionError(
                        "worker_exited",
                        f"Inference worker exited with code {process.exitcode}.",
                    )
        except (EOFError, BrokenPipeError, OSError) as exc:
            process = self._process
            exit_code = None
            if process is not None:
                process.join(timeout=0.2)
                exit_code = process.exitcode
            self.close()
            error = ExecutionError(
                "worker_exited", f"Inference IPC closed: {type(exc).__name__}; worker exit code={exit_code}."
            )
            error.context["worker_exit_code"] = exit_code
            raise error from exc
        except BaseException:
            self.close()
            raise
        finally:
            self._call_lock.release()

    def close(self) -> None:
        with self._state_lock:
            process, connection = self._process, self._connection
            self._process, self._connection = None, None
            self.profile_id, self.generation, self.loaded = None, None, False
            if process is not None:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        if process.is_alive():
                            process.kill()
                elif process.is_alive():
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=10,
                        check=False,
                    )
                process.join(timeout=5)
                if process.is_alive():
                    raise ExecutionError(
                        "worker_cleanup_failed",
                        "Inference worker did not stop; refusing further work.",
                    )
                process.close()
            if connection is not None:
                connection.close()
