from __future__ import annotations

import threading
import json
import inspect
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any, Callable

from .difficulty import POLICY_VERSION as DIFFICULTY_POLICY_VERSION
from .difficulty import OCRDifficultyAssessment, assess_ocr_difficulty
from .gpu_probe import format_probe, probe_gpu
from .job_registry import JobClaim, JobRegistry
from .model_registry import (
    ModelProfile,
    get_engine,
    resolve_model_reference,
    select_model_profile,
    select_model_profile_with_route,
)
from .objective_result import (
    annotate_result,
    caller_binding_sha256,
    file_sha256,
    write_objective_sidecar,
)
from .outputs import safe_output_stem, write_isolated_projections, write_outputs
from .pdf_utils import render_pdf_to_files
from .router import collect_files, is_pdf


HEAVY_ISOLATED_ENGINES = {"vl", "structure"}
AUTO_ROUTING_POLICY_VERSION = f"smart-router-v3:{DIFFICULTY_POLICY_VERSION}"


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
        gpu_lease_factory: Callable[[str], Any] | None = None,
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
        self._parent_manages_gpu_lease = gpu_lease_factory is not None
        self._gpu_lease_factory = gpu_lease_factory or (lambda _owner: nullcontext())

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

    def _process_heavy_isolated(
        self,
        path: Path,
        output_dir: Path,
        profile: ModelProfile,
        *,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
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
        if self._parent_manages_gpu_lease:
            cmd.append("--broker-lease-held-by-parent")
        if request_hash:
            cmd.extend(["--request-hash", request_hash])
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

        stem = safe_output_stem(path)
        if request_hash:
            json_path = self._project_path(output_dir) / f"{stem}.{request_hash[:32]}.json"
        else:
            # Direct callers without a registry request retain the legacy
            # child-output lookup for compatibility.
            json_path = self._project_path(output_dir) / f"{stem}.json"
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
            "expected_page_count": len(images),
            "pages": pages,
        }

    def _process_file_with_profile(self, path: Path, profile: ModelProfile) -> dict[str, Any]:
        engine = self._engine(profile)
        if is_pdf(path):
            result = self._ocr_pdf_with_engine(path, profile, engine)
        else:
            result = engine.predict_image(str(path))
        result["source_file"] = str(path)
        result["engine_key"] = profile.engine
        result["model_id"] = profile.id
        return result

    def process_file(
        self,
        path: Path,
        engine_choice: str = "auto",
        model_choice: str | None = None,
    ) -> dict[str, Any]:
        profile = select_model_profile(path, engine_choice=engine_choice, model_choice=model_choice)
        return self._process_file_with_profile(path, profile)

    def _process_selected_profile(
        self,
        file_path: Path,
        output_dir: Path,
        profile: ModelProfile,
        *,
        write_files: bool,
        request_hash: str | None = None,
    ) -> dict[str, Any]:
        if profile.engine not in HEAVY_ISOLATED_ENGINES:
            return self.process_file(file_path, profile.engine, profile.id)
        heavy_runner = self._process_heavy_isolated
        accepts_request_hash = "request_hash" in inspect.signature(heavy_runner).parameters
        heavy_kwargs = {"request_hash": request_hash} if accepts_request_hash else {}
        if write_files:
            return heavy_runner(file_path, output_dir, profile, **heavy_kwargs)

        runtime_dir = self.project_root / "_server"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"localocr-{profile.engine}-", dir=runtime_dir) as tmp:
            return heavy_runner(file_path, Path(tmp), profile, **heavy_kwargs)

    def process_inputs(
        self,
        inputs: list[str | Path],
        *,
        engine_choice: str = "auto",
        model_choice: str | None = None,
        recursive: bool = False,
        out_dir: str | Path | None = None,
        write_files: bool = True,
        caller_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        files = collect_files([str(p) for p in inputs], recursive)
        if not files:
            raise FileNotFoundError("未找到可识别文件，支持 png/jpg/jpeg/bmp/webp/tif/tiff/pdf。")

        output_dir = Path(out_dir) if out_dir is not None else Path("outputs/api")
        results: list[dict[str, Any]] = []
        lease_acquired = False
        with ExitStack() as lease_stack:
            for file_path in files:
                profile, route = select_model_profile_with_route(
                    file_path,
                    engine_choice=engine_choice,
                    model_choice=model_choice,
                )
                route_dict = route.to_dict()
                claim: JobClaim | None = None
                if write_files:
                    request = self.job_registry.build_request(
                        file_path,
                        profile,
                        output_dir,
                        request_variant=_request_variant(engine_choice, model_choice, caller_binding),
                    )
                    claim = self.job_registry.try_claim(request)
                    if claim.kind == "cache_hit":
                        cached = dict(claim.response or {})
                        cached.setdefault("route", route_dict)
                        results.append(cached)
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
                                "route": route_dict,
                                "results": [],
                            }
                        )
                        return response

                try:
                    if not lease_acquired:
                        lease_stack.enter_context(self._gpu_lease_factory("localocr"))
                        lease_acquired = True
                    with self._lock:
                        result = self._process_selected_profile(
                            file_path,
                            output_dir,
                            profile,
                            write_files=write_files,
                            request_hash=claim.request.job_key if claim is not None else None,
                        )
                        if _should_assess_auto_ocr(engine_choice, model_choice, profile):
                            preliminary = annotate_result(
                                result,
                                file_path,
                                processor=getattr(profile, "adapter", f"localocr.engine:{profile.engine}"),
                                model_id=profile.id,
                                pipeline_version=getattr(profile, "pipeline_version", "unknown"),
                                config=getattr(profile, "options", {}),
                                request_hash=claim.request.job_key if claim is not None else None,
                                caller_binding=caller_binding,
                                evidence_persisted=False,
                            )
                            assessment = assess_ocr_difficulty(result)
                            if preliminary["objective_outcome"] == "no_text_detected":
                                # Independent blank-image evidence is already
                                # sufficient; do not spend a second model pass
                                # merely because OCR returned no blocks.
                                result = preliminary
                                route_dict = _assessed_route(route_dict, assessment)
                                route_dict["signals"] = list(route_dict.get("signals") or []) + [
                                    "ocr_no_text_confirmed"
                                ]
                            elif assessment.should_escalate:
                                vl_profile = resolve_model_reference("vl")
                                result = self._process_selected_profile(
                                    file_path,
                                    output_dir,
                                    vl_profile,
                                    write_files=write_files,
                                    request_hash=claim.request.job_key if claim is not None else None,
                                )
                                profile = vl_profile
                                route_dict = _escalated_route(route_dict, assessment, vl_profile)
                            else:
                                route_dict = _assessed_route(route_dict, assessment)
                        result["route"] = route_dict
                        result = annotate_result(
                            result,
                            file_path,
                            processor=getattr(profile, "adapter", f"localocr.engine:{profile.engine}"),
                            model_id=profile.id,
                            pipeline_version=getattr(profile, "pipeline_version", "unknown"),
                            config=getattr(profile, "options", {}),
                            request_hash=claim.request.job_key if claim is not None else None,
                            caller_binding=caller_binding,
                            evidence_persisted=write_files,
                        )
                        if write_files:
                            objective_path, objective_sha256 = write_objective_sidecar(
                                result["objective_result"],
                                file_path,
                                output_dir,
                                request_hash=result["objective_result"]["identity"]["request_sha256"],
                            )
                            result["objective_result_file"] = str(objective_path)
                            result["objective_result_sha256"] = objective_sha256
                            paths = write_outputs(result, file_path, output_dir)
                            paths.update(
                                write_isolated_projections(
                                    result,
                                    file_path,
                                    output_dir,
                                    request_hash=result["objective_result"]["identity"]["request_sha256"],
                                )
                            )
                            paths["objective"] = objective_path
                            result["output_files"] = {k: str(v) for k, v in paths.items()}
                            result["output_file_sha256"] = {
                                key: file_sha256(path) for key, path in paths.items()
                            }
                            result["output_file_size_bytes"] = {
                                key: path.stat().st_size for key, path in paths.items()
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


def _request_variant(
    engine_choice: str,
    model_choice: str | None,
    caller_binding: dict[str, Any] | None = None,
) -> str:
    model = model_choice or "<default>"
    if engine_choice == "auto" and model_choice is None:
        variant = f"engine=auto;model={model};policy={AUTO_ROUTING_POLICY_VERSION}"
    else:
        variant = f"engine={engine_choice};model={model}"
    binding_hash = caller_binding_sha256(caller_binding)
    if binding_hash:
        variant += f";caller_binding={binding_hash}"
    return variant


def _should_assess_auto_ocr(
    engine_choice: str,
    model_choice: str | None,
    profile: ModelProfile,
) -> bool:
    return engine_choice == "auto" and model_choice is None and profile.engine == "ocr"


def _assessed_route(
    route: dict[str, Any],
    assessment: OCRDifficultyAssessment,
) -> dict[str, Any]:
    assessed = dict(route)
    assessed["initial_engine"] = route.get("effective_engine")
    assessed["escalated"] = False
    assessed["difficulty"] = assessment.to_dict()
    return assessed


def _escalated_route(
    route: dict[str, Any],
    assessment: OCRDifficultyAssessment,
    target_profile: ModelProfile,
) -> dict[str, Any]:
    escalated = _assessed_route(route, assessment)
    from_engine = str(route.get("effective_engine") or "ocr")
    from_model_id = route.get("model_id")
    escalated.update(
        {
            "effective_engine": target_profile.engine,
            "reason": "ocr_result_difficulty_prefers_vl",
            "route_reason": "ocr_result_difficulty_prefers_vl",
            "confidence": 0.9,
            "signals": list(route.get("signals") or [])
            + [f"ocr_difficulty:{reason}" for reason in assessment.reasons],
            "model_id": target_profile.id,
            "escalated": True,
            "escalation": {
                "from_engine": from_engine,
                "from_model_id": from_model_id,
                "to_engine": target_profile.engine,
                "to_model_id": target_profile.id,
            },
        }
    )
    return escalated
