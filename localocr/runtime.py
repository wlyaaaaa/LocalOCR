"""One supervised, warm inference process; no persistent work queue or service."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import signal
import subprocess
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


def _process_identity(pid: int) -> tuple[int, float] | None:
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return pid, process.create_time()
    except psutil.Error:
        return None


def _guard_process_group(parent_identity: tuple[int, float], worker_pid: int) -> None:
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
    from .model_registry import get_engine, resolve_model_reference

    profile = resolve_model_reference(payload["profile_id"])
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

        cache["gpu"] = format_probe(probe_gpu())
    emit({"stage": "loading_model", "gpu": cache.get("gpu")})

    from .pdf_utils import render_pdf_to_files, rendered_pdf_page_metadata
    from .router import is_pdf

    if "engine" not in cache:
        cache["engine"] = get_engine(profile.id, device=device)
    engine = cache["engine"]
    ensure = getattr(engine, "_ensure", None)
    if callable(ensure):
        ensure()
    emit({"stage": "recognizing", "loaded_model": profile.id, "gpu": cache.get("gpu")})
    source = Path(payload["path"])
    if is_pdf(source):
        emit({"stage": "rendering_pdf"})
        images = render_pdf_to_files(source, out_dir=Path(payload["tmp_dir"]))
        pages = []
        for index, image_path in enumerate(images):
            emit(
                {
                    "stage": "recognizing",
                    "completed_pages": index,
                    "total_pages": len(images),
                }
            )
            page_result = engine.predict_image(str(image_path))
            for page in page_result.get("pages", []):
                page["page_index"] = index
                page.update(rendered_pdf_page_metadata(image_path))
                pages.append(page)
        result = {
            "engine": engine.engine_name,
            "model": engine.model_name,
            "device": device,
            "expected_page_count": len(images),
            "pages": pages,
        }
        emit(
            {
                "stage": "recognized",
                "completed_pages": len(images),
                "total_pages": len(images),
            }
        )
    else:
        result = engine.predict_image(str(source))
    result.update(
        source_file=str(source), engine_key=profile.engine, model_id=profile.id
    )
    if uses_gpu:
        result["cuda_module_loading"] = os.environ.get("CUDA_MODULE_LOADING", "default")
    return result


def _worker_main(
    connection: Connection, parent_identity: tuple[int, float], predictor=None
) -> None:
    guard_pid = None
    if os.name == "posix":
        os.setsid()
        worker_pid = os.getpid()
        # A guard in the same dedicated process group is required: PDEATHSIG
        # alone kills only the direct worker, not any native-library children.
        guard_pid = os.fork()
        if guard_pid == 0:
            connection.close()
            _guard_process_group(parent_identity, worker_pid)
        try:
            ctypes.CDLL(None).prctl(1, signal.SIGKILL)
        except (AttributeError, OSError):
            pass
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

            def emit(event: dict) -> None:
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
        self.loaded = False
        self.generation: str | None = None
        self.peak_memory_bytes = 0
        self.gpu_summary: str | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def _start(self, profile_id: str) -> None:
        with self._state_lock:
            if self.pid is not None and self.profile_id == profile_id:
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
            self._start(payload["profile_id"])
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
                        raise ExecutionError("inference_failed", message["detail"])
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
            self.close()
            raise ExecutionError(
                "worker_exited", f"Inference IPC closed: {type(exc).__name__}."
            ) from exc
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
