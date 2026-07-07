from __future__ import annotations

import threading
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .gpu_probe import format_probe, probe_gpu
from .job_registry import JobClaim, JobRegistry
from .model_registry import ModelProfile, get_engine, select_model_profile
from .outputs import safe_output_stem, write_outputs
from .pdf_utils import render_pdf_to_files
from .router import collect_files, is_pdf


HEAVY_ISOLATED_ENGINES = {"vl", "structure"}


def run_isolated_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_sec: int,
) -> subprocess.CompletedProcess[str]:
    """Run a command in an isolated process group and clean it up on timeout."""
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_isolated_process(proc, cmd, timeout_sec)
        raise subprocess.TimeoutExpired(cmd, timeout_sec, output=stdout, stderr=stderr)

    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _terminate_isolated_process(
    proc: subprocess.Popen[str],
    cmd: list[str],
    timeout_sec: int,
) -> tuple[str, str]:
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        return stdout, stderr

    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        proc.terminate()

    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            proc.kill()
        return proc.communicate()


class OCRService:
    """Long-lived OCR runtime that keeps Paddle models warm in memory."""

    def __init__(
        self,
        *,
        device: str = "gpu:0",
        tmp_dir: str | Path = "_pdf_pages/api",
        job_dir: str | Path | None = None,
        probe_on_start: bool = True,
        isolated_timeout_sec: int = 3600,
    ) -> None:
        self.device = device
        self.project_root = Path(__file__).resolve().parent.parent
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.isolated_timeout_sec = isolated_timeout_sec
        self.job_registry = JobRegistry(job_dir if job_dir is not None else self.project_root / "_server" / "jobs")
        self._engine_cache: dict[str, Any] = {}
        self._engine_profiles: dict[str, ModelProfile] = {}
        self._lock = threading.RLock()
        self.gpu_info = probe_gpu() if probe_on_start else None

    @property
    def gpu_summary(self) -> str | None:
        if self.gpu_info is None:
            return None
        return format_probe(self.gpu_info)

    @property
    def loaded_engines(self) -> list[str]:
        return sorted({profile.engine for profile in self._engine_profiles.values()})

    @property
    def loaded_models(self) -> list[str]:
        return sorted(self._engine_cache)

    def _engine(self, profile: ModelProfile):
        if profile.id not in self._engine_cache:
            self._engine_cache[profile.id] = get_engine(profile.id, device=self.device)
            self._engine_profiles[profile.id] = profile
        return self._engine_cache[profile.id]

    def _project_path(self, path: Path) -> Path:
        return path if path.is_absolute() else self.project_root / path

    def _process_heavy_isolated(self, path: Path, output_dir: Path, profile: ModelProfile) -> dict[str, Any]:
        """Run heavy document engines in a child process to keep the API worker stable."""
        output_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.tmp_dir / f"{profile.engine}_subprocess"
        cmd = [
            sys.executable,
            "-m",
            "localocr.cli",
            str(path),
            "--engine",
            profile.engine,
            "--model",
            profile.id,
            "--out-dir",
            str(output_dir),
            "--tmp-dir",
            str(tmp_dir),
        ]
        completed = run_isolated_command(
            cmd,
            cwd=self.project_root,
            timeout_sec=self.isolated_timeout_sec,
        )
        if completed.returncode != 0:
            stdout_tail = completed.stdout[-2000:]
            stderr_tail = completed.stderr[-2000:]
            raise RuntimeError(
                f"{profile.engine} isolated subprocess failed "
                f"(exit={completed.returncode}). stdout_tail={stdout_tail!r} stderr_tail={stderr_tail!r}"
            )

        json_path = self._project_path(output_dir) / f"{safe_output_stem(path)}.json"
        if not json_path.exists():
            raise RuntimeError(
                f"{profile.engine} isolated subprocess finished but did not create JSON output: {json_path}"
            )
        result = json.loads(json_path.read_text(encoding="utf-8"))
        result["source_file"] = str(path)
        result["engine_key"] = profile.engine
        result["model_id"] = profile.id
        return result

    def _ocr_pdf_with_engine(self, pdf_path: Path, profile: ModelProfile, engine) -> dict[str, Any]:
        images = render_pdf_to_files(pdf_path, out_dir=self.tmp_dir)
        pages: list[dict[str, Any]] = []
        for i, img in enumerate(images):
            result = engine.predict_image(str(img))
            for page in result.get("pages", []):
                page["page_index"] = i
                pages.append(page)
        return {
            "engine": engine.engine_name,
            "model": engine.model_name,
            "model_id": profile.id,
            "device": engine.device,
            "pages": pages,
        }

    def process_file(
        self,
        path: Path,
        engine_choice: str = "auto",
        model_choice: str | None = None,
    ) -> dict[str, Any]:
        profile = select_model_profile(path, engine_choice=engine_choice, model_choice=model_choice)
        engine = self._engine(profile)
        if is_pdf(path):
            result = self._ocr_pdf_with_engine(path, profile, engine)
        else:
            result = engine.predict_image(str(path))
        result["source_file"] = str(path)
        result["engine_key"] = profile.engine
        result["model_id"] = profile.id
        return result

    def process_inputs(
        self,
        inputs: list[str | Path],
        *,
        engine_choice: str = "auto",
        model_choice: str | None = None,
        recursive: bool = False,
        out_dir: str | Path | None = None,
        write_files: bool = True,
    ) -> dict[str, Any]:
        files = collect_files([str(p) for p in inputs], recursive)
        if not files:
            raise FileNotFoundError("未找到可识别文件，支持 png/jpg/jpeg/bmp/webp/tif/tiff/pdf。")

        output_dir = Path(out_dir) if out_dir is not None else Path("outputs/api")
        results: list[dict[str, Any]] = []
        for file_path in files:
            profile = select_model_profile(file_path, engine_choice=engine_choice, model_choice=model_choice)
            claim: JobClaim | None = None
            if write_files:
                request = self.job_registry.build_request(file_path, profile, output_dir)
                claim = self.job_registry.try_claim(request)
                if claim.kind == "cache_hit":
                    results.append(claim.response or {})
                    continue
                if claim.kind == "active":
                    response = dict(claim.response or {})
                    response.update(
                        {
                            "count": 0,
                            "device": self.device,
                            "gpu": self.gpu_summary,
                            "loaded_engines": self.loaded_engines,
                            "loaded_models": self.loaded_models,
                            "results": [],
                        }
                    )
                    return response

            try:
                with self._lock:
                    if profile.engine in HEAVY_ISOLATED_ENGINES:
                        if write_files:
                            result = self._process_heavy_isolated(file_path, output_dir, profile)
                        else:
                            runtime_dir = self.project_root / "_server"
                            runtime_dir.mkdir(parents=True, exist_ok=True)
                            with tempfile.TemporaryDirectory(
                                prefix=f"localocr-{profile.engine}-", dir=runtime_dir
                            ) as tmp:
                                result = self._process_heavy_isolated(file_path, Path(tmp), profile)
                    else:
                        result = self.process_file(file_path, engine_choice, model_choice)
                    if write_files and "output_files" not in result:
                        paths = write_outputs(result, file_path, output_dir)
                        result["output_files"] = {k: str(v) for k, v in paths.items()}
                    elif write_files and "output_files" in result:
                        stem = safe_output_stem(file_path)
                        result["output_files"] = {
                            "txt": str(output_dir / f"{stem}.txt"),
                            "md": str(output_dir / f"{stem}.md"),
                            "json": str(output_dir / f"{stem}.json"),
                        }
                if claim is not None and claim.kind == "run":
                    result = self.job_registry.complete(claim, result)
                results.append(result)
            except Exception as exc:
                if claim is not None and claim.kind == "run":
                    self.job_registry.fail(claim, exc)
                raise
            finally:
                if claim is not None and claim.kind == "run":
                    self.job_registry.release(claim)
        return {
            "ok": True,
            "count": len(results),
            "device": self.device,
            "gpu": self.gpu_summary,
            "loaded_engines": self.loaded_engines,
            "loaded_models": self.loaded_models,
            "results": results,
        }
