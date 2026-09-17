from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from .model_registry import ModelProfile, resolve_model_reference
from .release_identity import execution_sha256
from .objective_result import (
    config_sha256,
    file_sha256,
    validate_objective_sidecar,
)

SCHEMA_VERSION = 1
CACHE_VERSION = 5
_LEGACY_PID_TIME_GRACE_SECONDS = 2.0
_REGISTRY_GUARD_NAME = ".job-registry.guard"


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
    request_variant: str = ""
    config_sha256: str = ""


@dataclass
class JobClaim:
    kind: str
    request: JobRequest
    manifest_path: Path
    lock_path: Path
    lock_fd: int | None = None
    response: dict[str, Any] | None = None
    execution_id: str | None = None


@contextmanager
def _exclusive_file_lock(fd: int):
    """Serialize stale-lock takeover across coordinator processes.

    LocalOCR executes its Python runtime in WSL, where ``flock`` is a native
    cross-process lock.  The small Windows branch keeps direct unit callers
    serialized too; neither branch introduces a work queue or a long-lived lock.
    """

    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("LocalOCR cannot atomically recover a stale lock on this platform.")


class JobRegistry:
    """File-backed OCR job registry with atomic lock files."""

    def __init__(
        self, job_dir: str | Path, *, stale_after_sec: int = 24 * 3600
    ) -> None:
        self.job_dir = Path(job_dir)
        self.stale_after_sec = stale_after_sec

    @contextmanager
    def _registry_guard(self):
        """One short-lived native guard for this directory's metadata commits.

        The guard file is deliberately retained as one tiny directory-level
        coordination primitive.  Data lock files are never advisory-locked or
        kept open, so Windows may safely re-read and unlink them.
        """

        self.job_dir.mkdir(parents=True, exist_ok=True)
        path = self.job_dir / _REGISTRY_GUARD_NAME
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            with _exclusive_file_lock(fd):
                yield
        finally:
            os.close(fd)

    def build_request(
        self,
        source_path: str | Path,
        profile: ModelProfile,
        output_dir: str | Path,
        *,
        request_variant: str = "",
    ) -> JobRequest:
        source = Path(source_path)
        stat = source.stat()
        source_hash = _file_sha256(source)
        after = source.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
            after.st_dev,
        ):
            raise ValueError("Source changed while computing the OCR request identity.")
        output = Path(output_dir)
        payload = {
            "cache_version": CACHE_VERSION,
            "execution_identity": execution_sha256(profile),
            "engine": profile.engine,
            "output_dir": _norm_path(output),
            "profile_id": profile.id,
            "adapter": profile.adapter,
            "pipeline_version": profile.pipeline_version,
            "config": profile.options,
            "request_variant": request_variant,
            "source_path": _norm_path(source),
            "source_sha256": source_hash,
            "source_size": stat.st_size,
        }
        job_key = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return JobRequest(
            job_key=job_key,
            job_id=job_key[:16],
            source_path=source,
            source_size=stat.st_size,
            source_sha256=source_hash,
            profile_id=profile.id,
            engine=profile.engine,
            output_dir=output,
            request_variant=request_variant,
            config_sha256=config_sha256(profile.options),
        )

    def try_claim(self, request: JobRequest) -> JobClaim:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self._manifest_path(request)
        lock_path = self._lock_path(request)
        with self._registry_guard():
            while True:
                cached = self._cache_hit_response(request, manifest_path)
                if cached is not None:
                    return JobClaim(
                        "cache_hit", request, manifest_path, lock_path, response=cached
                    )

                try:
                    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except FileExistsError:
                    cached = self._cache_hit_response(request, manifest_path)
                    if cached is not None:
                        return JobClaim(
                            "cache_hit", request, manifest_path, lock_path, response=cached
                        )
                    if self._lock_is_stale(lock_path, manifest_path):
                        lock_path.unlink(missing_ok=True)
                        continue
                    manifest = _read_json(manifest_path)
                    return JobClaim(
                        "active",
                        request,
                        manifest_path,
                        lock_path,
                        response=self._active_response(request, manifest),
                    )

                execution_id = uuid.uuid4().hex
                owner_start_time = psutil.Process(os.getpid()).create_time()
                owner = {
                    "owner_pid": os.getpid(),
                    "owner_start_time": owner_start_time,
                    "execution_id": execution_id,
                }
                try:
                    os.write(fd, json.dumps(owner).encode("utf-8"))
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
                            "request_variant": request.request_variant,
                            "config_sha256": request.config_sha256,
                            "output_dir": str(request.output_dir),
                            "started_at": _now_iso(),
                            "updated_at": _now_iso(),
                            **owner,
                        },
                    )
                except BaseException:
                    os.close(fd)
                    lock_path.unlink(missing_ok=True)
                    raise
                os.close(fd)
                return JobClaim(
                    "run",
                    request,
                    manifest_path,
                    lock_path,
                    execution_id=execution_id,
                )

    def cached_response(self, request: JobRequest) -> dict[str, Any] | None:
        return self._cache_hit_response(request, self._manifest_path(request))

    @contextmanager
    def _running_claim_fence(self, claim: JobClaim):
        """Serialize and verify the claim before publishing a state transition."""

        if claim.kind != "run" or not claim.execution_id:
            raise RuntimeError("OCR job claim has no execution identity.")
        with self._registry_guard():
            owner = _read_json(claim.lock_path)
            manifest = _read_json(claim.manifest_path)
            if (
                owner.get("execution_id") != claim.execution_id
                or manifest.get("execution_id") != claim.execution_id
                or manifest.get("status") != "running"
            ):
                raise RuntimeError("OCR job ownership changed during execution.")
            yield manifest

    def progress(self, claim: JobClaim | None, fields: dict) -> None:
        if claim is None or claim.kind != "run":
            return
        with self._running_claim_fence(claim) as manifest:
            manifest.update(fields)
            self._write_manifest(claim.manifest_path, manifest)

    def recover_abandoned(self) -> None:
        """Startup recovery only: a dead owner cannot hold a 24-hour phantom job."""
        if not self.job_dir.exists():
            return
        with self._registry_guard():
            for lock in self.job_dir.glob("*.lock"):
                manifest_path = lock.with_suffix(".json")
                if not self._lock_is_stale(lock, manifest_path):
                    continue
                manifest = _read_json(manifest_path)
                if manifest.get("status") == "running":
                    manifest.update(
                        status="failed",
                        stage="failed",
                        error_code="owner_exited",
                        error_tail="The previous OCR coordinator exited before completing this job.",
                        updated_at=_now_iso(),
                    )
                    self._write_manifest(manifest_path, manifest)
                lock.unlink(missing_ok=True)

    def complete(self, claim: JobClaim, result: dict[str, Any]) -> dict[str, Any]:
        stored = dict(result)
        stored["job_id"] = claim.request.job_id
        stored["job_key"] = claim.request.job_key
        stored["cache_status"] = "stored"
        with self._running_claim_fence(claim) as running:
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
                    "engine": stored.get("engine_key") or claim.request.engine,
                    "model_id": stored.get("model_id") or claim.request.profile_id,
                    "request_variant": claim.request.request_variant,
                    "config_sha256": claim.request.config_sha256,
                    "output_dir": str(claim.request.output_dir),
                    "started_at": running.get("started_at"),
                    "updated_at": _now_iso(),
                    "execution_id": claim.execution_id,
                    "output_files": stored.get("output_files") or {},
                    "output_file_sha256": stored.get("output_file_sha256") or {},
                    "output_file_size_bytes": stored.get("output_file_size_bytes") or {},
                    "result": stored,
                },
            )
        return stored

    def fail(
        self,
        claim: JobClaim,
        exc: BaseException,
        *,
        partial_output_files: dict | None = None,
    ) -> None:
        with self._running_claim_fence(claim) as running:
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
                    "request_variant": claim.request.request_variant,
                    "config_sha256": claim.request.config_sha256,
                    "output_dir": str(claim.request.output_dir),
                    "started_at": running.get("started_at"),
                    "updated_at": _now_iso(),
                    "execution_id": claim.execution_id,
                    "error_tail": f"{type(exc).__name__}: {exc}"[-2000:],
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "partial_output_files": partial_output_files or {},
                },
            )

    def release(self, claim: JobClaim) -> None:
        if claim.lock_fd is not None:
            try:
                os.close(claim.lock_fd)
            except OSError:
                pass
            claim.lock_fd = None
        with self._registry_guard():
            lock = _read_json(claim.lock_path)
            if claim.execution_id and lock.get("execution_id") != claim.execution_id:
                return
            claim.lock_path.unlink(missing_ok=True)

    def read_status(self, job_key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{64}", job_key):
            return {"ok": False, "status": "not_found", "job_key": job_key}
        manifest = _read_json(self.job_dir / f"{job_key}.json")
        if not manifest:
            return {"ok": False, "status": "not_found", "job_key": job_key}
        status = dict(manifest)
        status["ok"] = True
        status["cache_available"] = status.get(
            "status"
        ) == "completed" and self._cache_artifacts_valid(
            request=None,
            manifest=status,
        )
        return status

    def _manifest_path(self, request: JobRequest) -> Path:
        return self.job_dir / f"{request.job_key}.json"

    def _lock_path(self, request: JobRequest) -> Path:
        return self.job_dir / f"{request.job_key}.lock"

    def _cache_hit_response(
        self, request: JobRequest, manifest_path: Path
    ) -> dict[str, Any] | None:
        manifest = _read_json(manifest_path)
        if manifest.get("status") != "completed":
            return None
        output_files = manifest.get("output_files") or {}
        if not output_files or not self._cache_artifacts_valid(
            request=request, manifest=manifest
        ):
            return None
        result = dict(manifest.get("result") or {})
        result["job_id"] = request.job_id
        result["job_key"] = request.job_key
        result["cache_status"] = "cache_hit"
        result["output_files"] = output_files
        return result

    def _cache_artifacts_valid(
        self,
        *,
        request: JobRequest | None,
        manifest: dict[str, Any],
    ) -> bool:
        """Validate new objective receipts and projection hashes before reuse."""

        output_files = manifest.get("output_files") or {}
        if not _output_files_exist(output_files):
            return False
        result = manifest.get("result") or {}
        for model_id, identity in (result.get("execution_identities") or {}).items():
            try:
                if execution_sha256(resolve_model_reference(model_id)) != identity:
                    return False
            except (KeyError, ValueError, OSError):
                return False
        output_hashes = (
            result.get("output_file_sha256") or manifest.get("output_file_sha256") or {}
        )
        output_sizes = (
            result.get("output_file_size_bytes")
            or manifest.get("output_file_size_bytes")
            or {}
        )
        objective_path = output_files.get("objective")
        strict_objective = bool(objective_path or result.get("objective_result"))
        if strict_objective:
            # A modern objective receipt is only cacheable when every emitted
            # artifact is bound to a non-empty size and hash.  Missing maps or
            # extra/untracked paths fail closed instead of falling back to the
            # old path-exists check.
            if not isinstance(output_hashes, dict) or set(output_hashes) != set(
                output_files
            ):
                return False
            if not isinstance(output_sizes, dict) or set(output_sizes) != set(
                output_files
            ):
                return False
            for name, raw_path in output_files.items():
                path = Path(str(raw_path))
                expected_hash = output_hashes.get(name)
                expected_size = output_sizes.get(name)
                if (
                    not path.is_file()
                    or not isinstance(expected_hash, str)
                    or not expected_hash
                    or not isinstance(expected_size, int)
                    or expected_size <= 0
                ):
                    return False
                try:
                    if (
                        path.stat().st_size != expected_size
                        or file_sha256(path) != expected_hash
                    ):
                        return False
                except OSError:
                    return False
        else:
            # Legacy manifests remain readable with their historical
            # path-exists behavior; no objective sidecar means no modern
            # receipt contract is being claimed.
            if output_hashes:
                if not isinstance(output_hashes, dict):
                    return False
                for name, expected in output_hashes.items():
                    path = output_files.get(name)
                    if not path or not isinstance(expected, str):
                        return False
                    try:
                        if file_sha256(path) != expected:
                            return False
                    except OSError:
                        return False
            if output_sizes:
                if not isinstance(output_sizes, dict):
                    return False
                for name, expected in output_sizes.items():
                    path = output_files.get(name)
                    if not path or not isinstance(expected, int) or expected <= 0:
                        return False
                    try:
                        if Path(path).stat().st_size != expected:
                            return False
                    except OSError:
                        return False

        if not objective_path:
            # Legacy manifests remain readable.  New service results always
            # carry the sidecar, so they take the strict branch below.
            return not result.get("objective_result")
        if request is None:
            request_hash = str(manifest.get("job_key") or "")
            raw_sha256 = str(manifest.get("source_sha256") or "")
            source_size = manifest.get("source_size")
            profile_id = str(result.get("model_id") or manifest.get("model_id") or "")
            engine = str(result.get("engine_key") or manifest.get("engine") or "")
            config_hash = manifest.get("config_sha256")
            try:
                config_hash = config_sha256(resolve_model_reference(profile_id).options)
            except (KeyError, ValueError):
                pass
        else:
            request_hash = request.job_key
            raw_sha256 = request.source_sha256
            source_size = request.source_size
            # Auto routing may finish on VL after the request was initially
            # claimed for OCR.  Bind the receipt to the final stored profile,
            # while retaining the request job key as the idempotency identity.
            profile_id = str(result.get("model_id") or request.profile_id)
            engine = str(result.get("engine_key") or request.engine)
            config_hash = request.config_sha256
            try:
                config_hash = config_sha256(resolve_model_reference(profile_id).options)
            except (KeyError, ValueError):
                pass
        expected_objective_hash = (
            output_hashes.get("objective") if isinstance(output_hashes, dict) else None
        )
        return validate_objective_sidecar(
            objective_path,
            request_hash=request_hash,
            raw_sha256=raw_sha256,
            source_size=source_size,
            profile_id=profile_id,
            engine=engine,
            config_sha256_value=config_hash or None,
            expected_file_sha256=expected_objective_hash,
        )

    def _active_response(
        self, request: JobRequest, manifest: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "active_localocr_task",
            "error_code": "localocr_busy",
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
                "stage": manifest.get("stage"),
                "worker_pid": manifest.get("worker_pid"),
                "deadline_at": manifest.get("deadline_at"),
            },
        }

    def _lock_is_stale(self, lock_path: Path, manifest_path: Path) -> bool:
        try:
            lock_stat = lock_path.stat()
            age = time.time() - lock_stat.st_mtime
        except FileNotFoundError:
            return False
        # Prefer the lock owner: an older manifest can coexist briefly with a
        # newly acquired lock before its new running manifest is published.
        owner = _read_json(lock_path)
        if not owner:
            try:
                owner = {
                    "owner_pid": int(lock_path.read_text(encoding="utf-8").strip())
                }
            except (OSError, ValueError):
                if age < 5:
                    return False
                owner = _read_json(manifest_path)
        pid = owner.get("owner_pid")
        if isinstance(pid, int) and pid > 0:
            try:
                process = psutil.Process(pid)
                if process.status() == psutil.STATUS_ZOMBIE:
                    return True
                expected_start = owner.get("owner_start_time")
                if isinstance(expected_start, (int, float)):
                    return process.create_time() != expected_start
                # Legacy versions wrote only a raw PID.  A live PID alone is
                # not ownership proof after reboot: when the current process
                # demonstrably started after this lock was written, it is a
                # reused PID.  Ambiguous clocks/old live jobs remain fail-closed.
                return process.create_time() > (
                    lock_stat.st_mtime + _LEGACY_PID_TIME_GRACE_SECONDS
                )
            except psutil.NoSuchProcess:
                return True
            except psutil.AccessDenied:
                return False
        return age >= self.stale_after_sec

    def _write_manifest(self, manifest_path: Path, payload: dict[str, Any]) -> None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=manifest_path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            tmp_path = Path(handle.name)
        try:
            tmp_path.replace(manifest_path)
        finally:
            tmp_path.unlink(missing_ok=True)


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
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _claim_started_at(claim: JobClaim) -> str | None:
    value = _read_json(claim.manifest_path).get("started_at")
    return value if isinstance(value, str) and value else None


def _output_files_exist(output_files: dict[str, Any]) -> bool:
    for value in output_files.values():
        if not value or not Path(str(value)).exists():
            return False
    return True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
