from __future__ import annotations

import argparse
import os
import shutil
import tempfile
import threading
import psutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .path_utils import to_wsl_path
from .gpu_broker import (
    GpuBrokerConflict,
    GpuBrokerError,
    GpuBrokerLease,
    GpuBrokerLeaseLost,
)
from .observer import ObserverProjection
from .service import OCRService
from .runtime import DEFAULT_TIMEOUT_SEC, MAX_TIMEOUT_SEC, ExecutionError


class OCRPathRequest(BaseModel):
    path: str = Field(..., description="Windows or WSL path to image/PDF/folder")
    engine: Literal["auto", "ocr", "vl", "structure"] = "auto"
    model: str | None = Field(None, description="Concrete model profile id; optional")
    recursive: bool = False
    out_dir: str | None = None
    write_outputs: bool = True
    timeout_sec: float = Field(DEFAULT_TIMEOUT_SEC, gt=0, le=MAX_TIMEOUT_SEC)
    caller_binding: dict[str, Any] | None = Field(
        None,
        description="Opaque caller-owned binding to pass through unchanged; LocalOCR does not mint governance fields",
    )


@asynccontextmanager
async def lifespan(_app):
    global _service
    try:
        yield
    finally:
        if _service is not None:
            _service.close()
            _service = None


app = FastAPI(
    title="LocalOCR API",
    version="0.6.0",
    description="Local-only OCR API for PP-OCRv6_medium, PaddleOCR-VL-1.6, and PP-StructureV3.",
    lifespan=lifespan,
)

_service: OCRService | None = None
_service_lock = threading.Lock()


def get_service() -> OCRService:
    global _service
    with _service_lock:
        if _service is None:
            _service = OCRService(
                device="gpu:0",
                tmp_dir=Path(__file__).resolve().parent.parent / "_pdf_pages" / "api",
                probe_on_start=True,
                gpu_lease_factory=GpuBrokerLease,
            )
    return _service


@app.get("/health")
def health() -> dict:
    service = get_service()
    active = service.active_jobs
    failure = service.terminal_persistence_failure
    return {
        "ok": failure is None,
        "service": "localocr",
        "readiness": "job_state_persistence_failed" if failure else "ready",
        "recovery_job": {key: failure.get(key) for key in ("job_id", "job_key")} if failure else None,
        "api_version": "0.6.0",
        "server_pid": os.getpid(),
        "server_start_time": psutil.Process().create_time(),
        "gpu": service.gpu_summary,
        "gpu_status": "ready" if service.gpu_summary else "not_probed",
        "loaded_engines": service.loaded_engines,
        "loaded_models": service.loaded_models,
        "active_jobs": active,
        "active_jobs_count": len(active),
        "worker_pid": service._runtime.pid,
        "service_memory_peak_bytes": service._runtime.peak_memory_bytes,
        "service_memory_limit_bytes": service._runtime.memory_limit_bytes,
    }


@app.get("/jobs/{job_key}")
def job_status(job_key: str) -> dict:
    result = get_service().job_registry.read_status(job_key)
    return result if result.get("ok") else JSONResponse(result, status_code=404)


@app.post("/jobs/{job_key}/cancel")
def cancel_job(job_key: str):
    if get_service().cancel(job_key):
        return JSONResponse(
            {"ok": True, "status": "cancelling", "job_key": job_key}, status_code=202
        )
    result = get_service().job_registry.read_status(job_key)
    return JSONResponse(
        {"ok": False, "status": "not_running", "job_key": job_key},
        status_code=409 if result.get("ok") else 404,
    )


def get_observer_projection() -> ObserverProjection:
    if _service is not None:
        return ObserverProjection(_service.job_registry.job_dir)
    project_root = Path(__file__).resolve().parent.parent
    return ObserverProjection(project_root / "_server" / "jobs")


@app.get("/observer/jobs")
def observer_jobs(limit: int = 100) -> dict:
    return get_observer_projection().list_jobs(limit=limit)


@app.get("/observer/jobs/{job_id}")
def observer_job(job_id: str) -> dict:
    projected = get_observer_projection().get_job(job_id)
    if projected is None:
        raise HTTPException(status_code=404, detail="observer_job_not_found")
    return projected


@app.post("/ocr/path")
def ocr_path(req: OCRPathRequest):
    try:
        path = to_wsl_path(req.path)
        out_dir = to_wsl_path(req.out_dir) if req.out_dir else None
        service = get_service()
        result = service.process_inputs(
            [path],
            engine_choice=req.engine,
            model_choice=req.model,
            recursive=req.recursive,
            out_dir=out_dir,
            write_files=req.write_outputs,
            caller_binding=req.caller_binding,
            timeout_sec=req.timeout_sec,
        )
        return _process_response(result)
    except Exception as exc:
        return _error_response(exc)


def _process_response(result: dict):
    if result.get("status") == "active_localocr_task":
        return JSONResponse(result, status_code=409)
    return result


def _error_response(exc: Exception) -> JSONResponse:
    context = dict(getattr(exc, "localocr_context", {}) or {})
    if isinstance(exc, ExecutionError):
        code, status = exc.code, exc.http_status
        context.update(exc.context)
    elif isinstance(exc, GpuBrokerConflict):
        code, status = "gpu_busy", 409
        context.update(
            active_owner=exc.owner,
            active_jobs=get_service().active_jobs,
            recommendation="do_not_blindly_retry",
        )
    elif isinstance(exc, GpuBrokerLeaseLost):
        code, status = "gpu_lease_lost", 503
    elif isinstance(exc, GpuBrokerError):
        code, status = "broker_unavailable", 503
    elif isinstance(exc, FileNotFoundError):
        code, status = "input_not_found", 404
    elif isinstance(exc, PermissionError):
        code, status = "input_access_denied", 403
    elif isinstance(exc, ValueError):
        code, status = "invalid_request", 400
    else:
        code, status = "runtime_error", 500
    payload = {
        "ok": False,
        "status": "active_localocr_task"
        if code in {"gpu_busy", "localocr_busy"}
        else "failed",
        "error_code": code,
        "detail": f"{type(exc).__name__}: {exc}",
        **context,
    }
    return JSONResponse(payload, status_code=status)


@app.post("/ocr/file")
async def ocr_file(
    file: UploadFile = File(...),
    engine: Literal["auto", "ocr", "vl", "structure"] = "auto",
    model: str | None = None,
    write_outputs: bool = True,
    timeout_sec: float = Query(DEFAULT_TIMEOUT_SEC, gt=0, le=MAX_TIMEOUT_SEC),
):
    suffix = Path(file.filename or "upload").suffix or ".png"
    with tempfile.TemporaryDirectory(prefix="localocr-upload-") as tmp:
        target = Path(tmp) / f"upload{suffix}"
        with target.open("wb") as f:
            await run_in_threadpool(shutil.copyfileobj, file.file, f)
        try:
            service = get_service()
            result = await run_in_threadpool(
                service.process_inputs,
                [target],
                engine_choice=engine,
                model_choice=model,
                recursive=False,
                out_dir=Path("outputs/api_uploads"),
                write_files=write_outputs,
                timeout_sec=timeout_sec,
            )
            return _process_response(result)
        except Exception as exc:
            return _error_response(exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the LocalOCR local-only API server."
    )
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=18665)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("localocr.server:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
