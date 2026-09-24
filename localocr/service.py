from __future__ import annotations

import os

import math
from dataclasses import asdict
import shutil
import tempfile
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .difficulty import POLICY_VERSION as DIFFICULTY_POLICY_VERSION
from .difficulty import OCRDifficultyAssessment, assess_ocr_difficulty, assess_ocr_pages
from .gpu_broker import GpuBrokerLease
from .job_registry import JobClaim, JobRegistry
from .model_registry import (
    ModelProfile,
    load_model_profiles,
    resolve_model_reference,
    select_model_profile_with_route,
)
from .objective_result import (
    annotate_result,
    caller_binding_sha256,
    file_sha256,
    write_objective_sidecar,
)
from .outputs import build_display_summary, write_isolated_projections, write_outputs
from .router import collect_input_inventory
from .release_identity import execution_sha256
from .runtime import (
    DEFAULT_TIMEOUT_SEC,
    MAX_TIMEOUT_SEC,
    ExecutionError,
    InferenceRuntime,
)


AUTO_ROUTING_POLICY_VERSION = f"smart-router-v5:page-selective:{DIFFICULTY_POLICY_VERSION}"
OUTPUT_PROJECTION_VERSION = "display-summary-v1"


class OCRService:
    """Lightweight coordinator: one in-flight task and one replaceable warm worker."""

    def __init__(
        self,
        *,
        device: str = "gpu:0",
        tmp_dir: str | Path = "_pdf_pages/api",
        job_dir: str | Path | None = None,
        probe_on_start: bool = True,
        isolated_timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        gpu_lease_factory: Callable[[str], Any] | None = None,
        runtime: InferenceRuntime | None = None,
    ) -> None:
        self.device = device
        self.project_root = Path(os.environ.get("LOCALOCR_PROJECT_ROOT") or Path(__file__).resolve().parent.parent)
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self.isolated_timeout_sec = isolated_timeout_sec
        self.job_registry = JobRegistry(
            job_dir if job_dir is not None else self.project_root / "_server" / "jobs"
        )
        self.job_registry.recover_abandoned()
        self._runtime = runtime or InferenceRuntime()
        self._probe_gpu = probe_on_start
        self._gpu_lease_factory = gpu_lease_factory or (
            GpuBrokerLease
            if device.lower().startswith(("gpu", "cuda"))
            else lambda _owner: nullcontext()
        )
        self._execution_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._execution: dict[str, Any] | None = None
        self._terminal_persistence_failure: dict[str, Any] | None = None
        self._closed = False

    @property
    def gpu_summary(self) -> str | None:
        return self._runtime.gpu_summary

    @property
    def loaded_models(self) -> list[str]:
        return (
            [self._runtime.profile_id]
            if self._runtime.pid and self._runtime.loaded
            else []
        )

    @property
    def loaded_engines(self) -> list[str]:
        return [resolve_model_reference(model).engine for model in self.loaded_models]

    @property
    def active_jobs(self) -> list[dict[str, Any]]:
        with self._state_lock:
            return [dict(self._active)] if self._active is not None else []

    @property
    def terminal_persistence_failure(self) -> dict[str, Any] | None:
        """Read-only reason this coordinator has failed closed after a write fault."""

        with self._state_lock:
            return (
                dict(self._terminal_persistence_failure)
                if self._terminal_persistence_failure is not None
                else None
            )

    def _raise_if_terminal_persistence_failed(self) -> None:
        failure = self.terminal_persistence_failure
        if failure is None:
            return
        error = ExecutionError(
            "job_state_persistence_failed",
            "LocalOCR cannot safely accept work until its terminal job state is persisted and the coordinator is restarted.",
            http_status=503,
        )
        error.context.update(failure)
        raise error

    def cancel(self, job_key: str) -> bool:
        with self._state_lock:
            if self._active is None or self._active["job_key"] != job_key:
                return False
            self._execution["cancel"].set()
            self._active["stage"] = "cancelling"
            return True

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            if self._execution is not None:
                self._execution["cancel"].set()
        self._runtime.close()

    def _busy_response(self, results: list[dict] | None = None) -> dict:
        active = self.active_jobs
        return {
            "ok": False,
            "status": "active_localocr_task",
            "error_code": "localocr_busy",
            "detail": "Another LocalOCR task is already running.",
            "recommendation": "do_not_blindly_retry",
            "active_jobs": active,
            "active_jobs_count": len(active),
            "job_id": active[0]["job_id"] if active else None,
            "job_key": active[0]["job_key"] if active else None,
            "results": results or [],
            "count": len(results or []),
        }

    def _progress(self, event: dict) -> None:
        safe_fields = {
            key: event[key]
            for key in (
                "stage",
                "worker_pid",
                "worker_generation",
                "completed_pages",
                "total_pages",
            )
            if key in event
        }
        with self._state_lock:
            if self._active is None:
                return
            self._active.update(safe_fields, updated_at=_now_iso())
            claim = self._execution["claim"]
            snapshot = dict(self._active)
        self.job_registry.progress(claim, snapshot)

    def _checkpoint(self) -> None:
        context = self._execution
        if context is None:
            raise ExecutionError("execution_missing", "No active execution context.")
        if context["cancel"].is_set() or self._closed:
            raise ExecutionError(
                "execution_cancelled", "The OCR task was cancelled.", http_status=409
            )
        if time.monotonic() >= context["deadline"]:
            raise ExecutionError(
                "execution_timeout",
                "The OCR execution deadline was exceeded.",
                http_status=504,
            )
        check = getattr(context.get("lease"), "raise_if_lost", None)
        if callable(check):
            check()

    def process_file(
        self, path: Path, engine_choice: str = "auto", model_choice: str | None = None
    ) -> dict:
        context = self._execution
        if context is None or context["thread_id"] != threading.get_ident():
            response = self.process_inputs(
                [path],
                engine_choice=engine_choice,
                model_choice=model_choice,
                write_files=False,
            )
            if not response["ok"]:
                raise ExecutionError(
                    "localocr_busy",
                    "Another LocalOCR task is running.",
                    http_status=409,
                )
            return response["results"][0]
        profile = context["profiles"].get(model_choice or engine_choice) or resolve_model_reference(model_choice or engine_choice)
        self._progress({"stage": "starting_worker"})
        with self._state_lock:
            self._active.update(engine=profile.engine, model_id=profile.id)
        lease = context.get("lease")
        payload = {
            "path": str(path),
            "profile_id": profile.id,
            "model_profile": asdict(profile),
            "profile_revision": context["identities"][profile.id],
            "page_indices": context.get("page_indices"),
            "device": self.device,
            "tmp_dir": str(context["scratch"] / "pages"),
            "probe_gpu": self._probe_gpu,
            "source_sha256": context["source_sha256"],
            "checkpoint_dir": str(context["scratch"] / "checkpoints" / context["identities"][profile.id]),
            "lease": getattr(lease, "worker_binding", None),
        }
        try:
            return self._runtime.predict(
                payload, deadline=context["deadline"], cancel=context["cancel"],
                check_lease=self._checkpoint, progress=self._progress,
            )
        except ExecutionError as exc:
            if not exc.context.get("partial_result"):
                from .checkpoints import read_partial
                partial = read_partial(Path(payload["checkpoint_dir"]), payload)
                if partial:
                    exc.context["partial_result"] = partial
            raise

    def _annotate(
        self,
        result: dict,
        source: Path,
        profile: ModelProfile,
        job_key: str,
        caller_binding,
        *,
        persisted: bool,
    ) -> dict:
        annotated = annotate_result(
            result,
            source,
            processor=getattr(profile, "adapter", f"localocr.engine:{profile.engine}"),
            model_id=profile.id,
            pipeline_version=getattr(profile, "pipeline_version", "unknown"),
            config=getattr(profile, "options", {}),
            request_hash=job_key,
            caller_binding=caller_binding,
            evidence_persisted=persisted,
            execution_status=result.get("execution_status", "completed"),
            failure=result.get("failure"),
        )
        annotated["execution_identities"] = dict(result.get("execution_identities") or {})
        annotated["display_summary"] = build_display_summary(annotated)
        return annotated

    def _persist(
        self, result: dict, source: Path, output_dir: Path, job_key: str
    ) -> dict:
        """Only request-bound artifacts are authoritative; stem files are display aliases."""
        objective_path, objective_hash = write_objective_sidecar(
            result["objective_result"],
            source,
            output_dir,
            request_hash=job_key,
        )
        result["objective_result_file"] = str(objective_path)
        result["objective_result_sha256"] = objective_hash
        paths = write_isolated_projections(
            result, source, output_dir, request_hash=job_key
        )
        paths.update(
            txt=paths["canonical_txt"],
            md=paths["canonical_md"],
            json=paths["canonical_json"],
            objective=objective_path,
        )
        result["output_files"] = {key: str(value) for key, value in paths.items()}
        result["output_file_sha256"] = {
            key: file_sha256(value) for key, value in paths.items()
        }
        result["output_file_size_bytes"] = {
            key: value.stat().st_size for key, value in paths.items()
        }
        write_outputs(result, source, output_dir)
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
        caller_binding: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.isolated_timeout_sec if timeout_sec is None else timeout_sec
        if (
            isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or not 0 < timeout <= MAX_TIMEOUT_SEC
        ):
            raise ValueError(
                f"timeout_sec must be greater than zero and at most {MAX_TIMEOUT_SEC:g}."
            )
        self._raise_if_terminal_persistence_failed()
        request_started = datetime.now(timezone.utc)
        request_deadline = time.monotonic() + timeout
        files, skipped_inputs = collect_input_inventory([str(path) for path in inputs], recursive)
        if not files:
            raise FileNotFoundError("No supported image or PDF was found.")
        output_dir = (
            Path(out_dir)
            if out_dir is not None
            else self.project_root / "outputs" / "api"
        )
        output_dir = output_dir.resolve()
        results: list[dict[str, Any]] = []
        for source in files:
            if time.monotonic() >= request_deadline:
                error = ExecutionError(
                    "execution_timeout",
                    "The OCR request deadline was exceeded.",
                    http_status=504,
                )
                error.context.update(results=results, completed_count=len(results))
                raise error
            if self._closed:
                raise ExecutionError(
                    "service_stopping", "LocalOCR is stopping.", http_status=503
                )
            registry = load_model_profiles()
            profile, route = select_model_profile_with_route(
                source, engine_choice=engine_choice, model_choice=model_choice, registry=registry
            )
            profiles = {profile.id: profile}
            if engine_choice == "auto" and model_choice is None:
                fallback = resolve_model_reference("vl", registry)
                profiles[fallback.id] = fallback
            identities = {key: execution_sha256(value) for key, value in profiles.items()}

            request = self.job_registry.build_request(
                source,
                profile,
                output_dir,
                request_variant=_request_variant(
                    engine_choice, model_choice, caller_binding, device=self.device,
                    fallback_identity=identities.get(registry.defaults["vl"]),
                ),
            )
            if write_files:
                cached = self.job_registry.cached_response(request)
                if cached is not None:
                    results.append(cached)
                    continue
            if not self._execution_lock.acquire(blocking=False):
                return self._busy_response(results)
            claim: JobClaim | None = None
            release_claim = False
            try:
                claim = self.job_registry.try_claim(request) if write_files else None
                if claim is not None and claim.kind == "cache_hit":
                    results.append(dict(claim.response or {}))
                    continue
                if claim is not None and claim.kind == "active":
                    return {
                        **dict(claim.response or {}),
                        "count": len(results),
                        "results": results,
                    }
                release_claim = claim is not None and claim.kind == "run"
                now = datetime.now(timezone.utc)
                with self._state_lock:
                    self._execution = {
                        "claim": claim,
                        "profiles": profiles,
                        "identities": identities,
                        "source_sha256": request.source_sha256,
                        "cancel": threading.Event(),
                        "deadline": request_deadline,
                        "thread_id": threading.get_ident(),
                        "lease": None,
                    }
                    self._active = {
                        "job_id": request.job_id,
                        "job_key": request.job_key,
                        "status": "running",
                        "stage": "acquiring_gpu",
                        "engine": profile.engine,
                        "model_id": profile.id,
                        "source_file": str(source),
                        "started_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                        "timeout_sec": timeout,
                        "deadline_at": (
                            request_started + timedelta(seconds=timeout)
                        ).isoformat(),
                        "worker_pid": None,
                    }
                initial: dict | None = None
                initial_profile = profile
                route_dict = route.to_dict()
                try:
                    self._progress({"stage": "acquiring_gpu"})
                    with self._gpu_lease_factory("localocr") as lease:
                        self._execution["lease"] = lease
                        with tempfile.TemporaryDirectory(
                            prefix=f"{request.job_id}-", dir=self.tmp_dir
                        ) as scratch:
                            scratch_path = Path(scratch)
                            self._execution["scratch"] = scratch_path
                            self._progress({"stage": "snapshotting_input"})
                            snapshot = scratch_path / source.name
                            shutil.copyfile(source, snapshot)
                            if (
                                snapshot.stat().st_size != request.source_size
                                or file_sha256(snapshot) != request.source_sha256
                            ):
                                raise ExecutionError(
                                    "source_changed",
                                    "Source changed while preparing the OCR input.",
                                    http_status=409,
                                )
                            self._checkpoint()
                            result = self.process_file(
                                snapshot, profile.engine, profile.id
                            )
                            result["source_file"] = str(source)
                            if _should_assess_auto_ocr(
                                engine_choice, model_choice, profile
                            ):
                                initial = self._annotate(
                                    result,
                                    snapshot,
                                    profile,
                                    request.job_key,
                                    caller_binding,
                                    persisted=False,
                                )
                                assessment = assess_ocr_difficulty(result)
                                route_dict = _assessed_route(route_dict, assessment)
                                page_assessments = assess_ocr_pages(result)
                                route_dict["page_assessments"] = page_assessments
                                escalate_indices = [p["page_index"] for p in page_assessments if p["should_escalate"]]
                                if initial["objective_outcome"] == "no_text_detected":
                                    route_dict["signals"] = list(
                                        route_dict.get("signals") or []
                                    ) + ["ocr_no_text_confirmed"]
                                elif escalate_indices:
                                    profile = fallback
                                    route_dict = _escalated_route(
                                        route_dict, assessment, profile
                                    )
                                    route_dict["escalated_page_indices"] = escalate_indices
                                    self._progress({"stage": "escalating"})
                                    self._execution["page_indices"] = escalate_indices
                                    second = self.process_file(snapshot, profile.engine, profile.id)
                                    result = _merge_escalated_pages(result, second, escalate_indices, initial_profile, profile)
                                    result["source_file"] = str(source)
                            self._checkpoint()
                            if (
                                not source.is_file()
                                or source.stat().st_size != request.source_size
                                or file_sha256(source) != request.source_sha256
                            ):
                                raise ExecutionError(
                                    "source_changed",
                                    "Original source changed during OCR; result was not published.",
                                    http_status=409,
                                )
                            result["execution_identities"] = identities
                            result["route"] = route_dict
                            result = self._annotate(
                                result,
                                source,
                                profile,
                                request.job_key,
                                caller_binding,
                                persisted=write_files,
                            )
                            if write_files:
                                self._progress({"stage": "publishing"})
                                self._checkpoint()
                                result = self._persist(
                                    result, source, output_dir, request.job_key
                                )
                            self._checkpoint()
                    # Completed is the commit point, after resource release.
                    if claim is not None:
                        result = self.job_registry.complete(claim, result)
                    else:
                        result.update(
                            job_id=request.job_id,
                            job_key=request.job_key,
                            cache_status="not_written",
                        )
                    results.append(result)
                except BaseException as exc:
                    self._runtime.close()
                    partial_outputs = None
                    worker_partial = exc.context.pop("partial_result", None) if isinstance(exc, ExecutionError) else None
                    if initial is None and worker_partial is not None:
                        initial = worker_partial
                        initial_profile = profile
                    if initial is not None and write_files:
                        if (
                            source.is_file()
                            and file_sha256(source) == request.source_sha256
                        ):
                            try:
                                initial["execution_status"] = "cancelled" if getattr(exc, "code", "") == "execution_cancelled" else "failed"
                                initial["failure"] = {"code": getattr(exc, "code", type(exc).__name__), "detail": str(exc)}
                                initial["execution_identities"] = identities
                                initial["route"] = {
                                    **route_dict,
                                    "effective_engine": initial_profile.engine,
                                    "model_id": initial_profile.id,
                                    "escalation_failed": profile.id != initial_profile.id,
                                    "escalation_error_code": getattr(
                                        exc, "code", type(exc).__name__
                                    ),
                                }
                                initial = self._annotate(
                                    initial,
                                    source,
                                    initial_profile,
                                    request.job_key,
                                    caller_binding,
                                    persisted=True,
                                )
                                partial = self._persist(
                                    initial,
                                    source,
                                    output_dir / "partial" / request.job_key,
                                    request.job_key,
                                )
                                partial_outputs = partial["output_files"]
                            except Exception:
                                pass  # Preserve the original execution failure.
                    if claim is not None:
                        try:
                            self.job_registry.fail(
                                claim, exc, partial_output_files=partial_outputs
                            )
                        except BaseException as persistence_exc:
                            release_claim = False
                            failure = {
                                "job_id": request.job_id,
                                "job_key": request.job_key,
                                "original_error_code": getattr(
                                    exc, "code", type(exc).__name__
                                ),
                                "original_error": f"{type(exc).__name__}: {exc}"[-2000:],
                                "persistence_error": (
                                    f"{type(persistence_exc).__name__}: {persistence_exc}"[-2000:]
                                ),
                            }
                            with self._state_lock:
                                self._terminal_persistence_failure = failure
                            terminal_error = ExecutionError(
                                "job_state_persistence_failed",
                                "LocalOCR could not persist the terminal job state; the coordinator is now fail-closed.",
                                http_status=503,
                            )
                            terminal_error.context.update(failure)
                            raise terminal_error from persistence_exc
                    if isinstance(exc, ExecutionError):
                        exc.context.update(
                            job_id=request.job_id,
                            job_key=request.job_key,
                            partial_output_files=partial_outputs,
                            results=results,
                            completed_count=len(results),
                            failed_input=str(source),
                            unprocessed_inputs=[str(p) for p in files[len(results)+1:]],
                            retryable=False if exc.code == "execution_cancelled" else None,
                        )
                    else:
                        exc.localocr_context = {
                            "job_id": request.job_id,
                            "job_key": request.job_key,
                        }
                    raise
            finally:
                if release_claim:
                    self.job_registry.release(claim)
                with self._state_lock:
                    self._active = None
                    self._execution = None
                self._execution_lock.release()
        return {
            "ok": True,
            "batch_coverage": "partial" if skipped_inputs else "complete",
            "skipped_inputs": skipped_inputs,
            "count": len(results),
            "device": self.device,
            "gpu": self.gpu_summary,
            "loaded_engines": self.loaded_engines,
            "loaded_models": self.loaded_models,
            "results": results,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_variant(
    engine_choice: str,
    model_choice: str | None,
    caller_binding: dict | None = None,
    *,
    device: str = "gpu:0",
    fallback_identity: str | None = None,
) -> str:
    model = model_choice or "<default>"
    variant = (
        f"engine={engine_choice};model={model};device={_normalized_device(device)}"
        f";output={OUTPUT_PROJECTION_VERSION}"
    )
    if engine_choice == "auto" and model_choice is None:
        variant += f";policy={AUTO_ROUTING_POLICY_VERSION}"
        variant += f";fallback={fallback_identity or execution_sha256(resolve_model_reference('vl'))}"
    binding_hash = caller_binding_sha256(caller_binding)
    if binding_hash:
        variant += f";caller_binding={binding_hash}"
    return variant


def _normalized_device(device: str) -> str:
    """Keep cache identity stable for spelling variants of the same device."""

    normalized = "".join(str(device).strip().casefold().split())
    if normalized in {"gpu", "cuda"}:
        return "gpu:0"
    if normalized.startswith("cuda:"):
        normalized = f"gpu:{normalized.removeprefix('cuda:')}"
    if normalized.startswith("gpu:"):
        suffix = normalized.removeprefix("gpu:")
        if suffix.isdecimal():
            return f"gpu:{int(suffix)}"
    return normalized


def _should_assess_auto_ocr(
    engine_choice: str, model_choice: str | None, profile: ModelProfile
) -> bool:
    return engine_choice == "auto" and model_choice is None and profile.engine == "ocr"


def _assessed_route(route: dict, assessment: OCRDifficultyAssessment) -> dict:
    return {
        **route,
        "initial_engine": route.get("effective_engine"),
        "escalated": False,
        "difficulty": assessment.to_dict(),
    }


def _escalated_route(
    route: dict, assessment: OCRDifficultyAssessment, target_profile: ModelProfile
) -> dict:
    return {
        **_assessed_route(route, assessment),
        "effective_engine": target_profile.engine,
        "reason": "ocr_result_difficulty_prefers_vl",
        "route_reason": "ocr_result_difficulty_prefers_vl",
        "confidence": None,
        "confidence_kind": "uncalibrated_policy",
        "signals": list(route.get("signals") or [])
        + [f"ocr_difficulty:{reason}" for reason in assessment.reasons],
        "model_id": target_profile.id,
        "escalated": True,
        "escalation": {
            "from_engine": str(route.get("effective_engine") or "ocr"),
            "from_model_id": route.get("model_id"),
            "to_engine": target_profile.engine,
            "to_model_id": target_profile.id,
        },
    }


def _merge_escalated_pages(first, second, indices, first_profile, second_profile):
    first_pages = first.get("pages") or []
    expected_indices = [p.get("page_index", i) for i, p in enumerate(first_pages)]
    replacements = second.get("pages") or []
    observed = [p.get("page_index") for p in replacements]
    if sorted(observed) != sorted(indices) or len(set(observed)) != len(observed):
        raise ExecutionError("page_coverage_mismatch", "Second-pass page indices do not match the requested subset")
    by_index = {p["page_index"]: p for p in replacements}
    pages = []
    for index, page in zip(expected_indices, first_pages, strict=True):
        if index in by_index:
            replacement = dict(by_index[index])
            replacement["first_pass_evidence"] = {
                "model_id": first_profile.id, "blocks": page.get("blocks") or [],
                "coordinate_reference": page.get("coordinate_reference"),
            }
            replacement.update(model_id=second_profile.id, engine_key=second_profile.engine)
            pages.append(replacement)
        else:
            pages.append({**page, "model_id": first_profile.id, "engine_key": first_profile.engine})
    return {**first, "pages": pages, "model_id": second_profile.id,
            "engine_key": second_profile.engine, "model": second.get("model"),
            "engine": second.get("engine"), "models_used": [first_profile.id, second_profile.id],
            "mixed_page_models": len(indices) != len(first_pages)}
