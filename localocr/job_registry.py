from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model_registry import ModelProfile

SCHEMA_VERSION = 1
CACHE_VERSION = 1


@dataclass(frozen=True)
class JobRequest:
    job_key: str
    job_id: str
    source_path: Path
    source_size: int
    source_sha256: str
    profile_id: str
    engine: str
    output_dir: Path


@dataclass
class JobClaim:
    kind: str
    request: JobRequest
    manifest_path: Path
    lock_path: Path
    lock_fd: int | None = None
    response: dict[str, Any] | None = None


class JobRegistry:
    """File-backed OCR job registry with atomic lock files."""

    def __init__(self, job_dir: str | Path, *, stale_after_sec: int = 24 * 3600) -> None:
        self.job_dir = Path(job_dir)
        self.stale_after_sec = stale_after_sec

    def build_request(self, source_path: str | Path, profile: ModelProfile, output_dir: str | Path) -> JobRequest:
        source = Path(source_path)
        stat = source.stat()
        source_hash = _file_sha256(source)
        output = Path(output_dir)
        payload = {
            "cache_version": CACHE_VERSION,
            "engine": profile.engine,
            "output_dir": _norm_path(output),
            "profile_id": profile.id,
            "source_path": _norm_path(source),
            "source_sha256": source_hash,
            "source_size": stat.st_size,
        }
        job_key = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return JobRequest(
            job_key=job_key,
            job_id=job_key[:16],
            source_path=source,
            source_size=stat.st_size,
            source_sha256=source_hash,
            profile_id=profile.id,
            engine=profile.engine,
            output_dir=output,
        )

    def try_claim(self, request: JobRequest) -> JobClaim:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._manifest_path(request)
        lock_path = self._lock_path(request)

        cached = self._cache_hit_response(request, manifest_path)
        if cached is not None:
            return JobClaim("cache_hit", request, manifest_path, lock_path, response=cached)

        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            cached = self._cache_hit_response(request, manifest_path)
            if cached is not None:
                return JobClaim("cache_hit", request, manifest_path, lock_path, response=cached)
            if self._lock_is_stale(lock_path, manifest_path):
                lock_path.unlink(missing_ok=True)
                return self.try_claim(request)
            manifest = _read_json(manifest_path)
            return JobClaim("active", request, manifest_path, lock_path, response=self._active_response(request, manifest))

        os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        self._write_manifest(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": request.job_id,
                "job_key": request.job_key,
                "status": "running",
                "source_path": str(request.source_path),
                "source_size": request.source_size,
                "source_sha256": request.source_sha256,
                "engine": request.engine,
                "model_id": request.profile_id,
                "output_dir": str(request.output_dir),
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "owner_pid": os.getpid(),
            },
        )
        return JobClaim("run", request, manifest_path, lock_path, lock_fd=fd)

    def complete(self, claim: JobClaim, result: dict[str, Any]) -> dict[str, Any]:
        stored = dict(result)
        stored["job_id"] = claim.request.job_id
        stored["job_key"] = claim.request.job_key
        stored["cache_status"] = "stored"
        self._write_manifest(
            claim.manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": claim.request.job_id,
                "job_key": claim.request.job_key,
                "status": "completed",
                "source_path": str(claim.request.source_path),
                "source_size": claim.request.source_size,
                "source_sha256": claim.request.source_sha256,
                "engine": claim.request.engine,
                "model_id": claim.request.profile_id,
                "output_dir": str(claim.request.output_dir),
                "updated_at": _now_iso(),
                "output_files": stored.get("output_files") or {},
                "result": stored,
            },
        )
        return stored

    def fail(self, claim: JobClaim, exc: BaseException) -> None:
        self._write_manifest(
            claim.manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "job_id": claim.request.job_id,
                "job_key": claim.request.job_key,
                "status": "failed",
                "source_path": str(claim.request.source_path),
                "source_size": claim.request.source_size,
                "source_sha256": claim.request.source_sha256,
                "engine": claim.request.engine,
                "model_id": claim.request.profile_id,
                "output_dir": str(claim.request.output_dir),
                "updated_at": _now_iso(),
                "error_tail": f"{type(exc).__name__}: {exc}"[-2000:],
            },
        )

    def release(self, claim: JobClaim) -> None:
        if claim.lock_fd is not None:
            try:
                os.close(claim.lock_fd)
            except OSError:
                pass
            claim.lock_fd = None
        claim.lock_path.unlink(missing_ok=True)

    def read_status(self, job_key: str) -> dict[str, Any]:
        manifest = _read_json(self.job_dir / f"{job_key}.json")
        if not manifest:
            return {"ok": False, "status": "not_found", "job_key": job_key}
        status = dict(manifest)
        status["ok"] = True
        status["cache_available"] = status.get("status") == "completed" and _output_files_exist(
            status.get("output_files") or {}
        )
        return status

    def _manifest_path(self, request: JobRequest) -> Path:
        return self.job_dir / f"{request.job_key}.json"

    def _lock_path(self, request: JobRequest) -> Path:
        return self.job_dir / f"{request.job_key}.lock"

    def _cache_hit_response(self, request: JobRequest, manifest_path: Path) -> dict[str, Any] | None:
        manifest = _read_json(manifest_path)
        if manifest.get("status") != "completed":
            return None
        output_files = manifest.get("output_files") or {}
        if not output_files or not _output_files_exist(output_files):
            return None
        result = dict(manifest.get("result") or {})
        result["job_id"] = request.job_id
        result["job_key"] = request.job_key
        result["cache_status"] = "cache_hit"
        result["output_files"] = output_files
        return result

    def _active_response(self, request: JobRequest, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "active_localocr_task",
            "recommendation": "do_not_blindly_retry",
            "job_id": request.job_id,
            "job_key": request.job_key,
            "source_file": str(request.source_path),
            "engine_key": request.engine,
            "model_id": request.profile_id,
            "active_job": {
                "started_at": manifest.get("started_at"),
                "updated_at": manifest.get("updated_at"),
                "owner_pid": manifest.get("owner_pid"),
                "manifest": str(self._manifest_path(request)),
            },
        }

    def _lock_is_stale(self, lock_path: Path, manifest_path: Path) -> bool:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return False
        if age < self.stale_after_sec:
            return False
        manifest = _read_json(manifest_path)
        owner_pid = manifest.get("owner_pid")
        if isinstance(owner_pid, int) and _pid_exists(owner_pid):
            return False
        return True

    def _write_manifest(self, manifest_path: Path, payload: dict[str, Any]) -> None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = manifest_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(manifest_path)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _output_files_exist(output_files: dict[str, Any]) -> bool:
    for value in output_files.values():
        if not value or not Path(str(value)).exists():
            return False
    return True


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
