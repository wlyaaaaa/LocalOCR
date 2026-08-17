from __future__ import annotations

"""Objective media-result semantics and the cache-verifiable sidecar.

The legacy TXT/Markdown/JSON projections are intentionally left intact.  This
module adds a small, versioned result contract so an empty OCR block list is
not silently interpreted as proof that the image contains no text.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


OBJECTIVE_SCHEMA = "media.objective-result.v1"
NEGATIVE_EVIDENCE_SCHEMA = "media.no-text-evidence.v1"
TEXT_DETECTION_STATUSES = {"text_detected", "no_text_detected", "indeterminate"}
EXECUTION_STATUSES = {"completed", "failed", "unsupported", "corrupt"}
LOW_CONFIDENCE_THRESHOLD = 0.50


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for identity hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def config_sha256(config: Mapping[str, Any] | None) -> str:
    return sha256_text(canonical_json(dict(config or {})))


def caller_binding_sha256(caller_binding: Mapping[str, Any] | None) -> str | None:
    if caller_binding is None:
        return None
    return sha256_text(canonical_json(dict(caller_binding)))


def derive_request_hash(
    source_path: str | Path,
    *,
    processor: str,
    model_id: str,
    pipeline_version: str,
    config: Mapping[str, Any] | None = None,
    request_variant: str = "",
    caller_binding: Mapping[str, Any] | None = None,
) -> str:
    """Derive a request identity when the API job registry is not involved."""

    source = Path(source_path)
    try:
        source_size = source.stat().st_size
        source_hash = file_sha256(source)
    except OSError:
        source_size = None
        source_hash = None
    payload = {
        "schema": OBJECTIVE_SCHEMA,
        "source_size": source_size,
        "raw_sha256": source_hash,
        "processor": processor,
        "model_id": model_id,
        "pipeline_version": pipeline_version,
        "config_sha256": config_sha256(config),
        "request_variant": request_variant,
        "caller_binding_sha256": caller_binding_sha256(caller_binding),
    }
    return sha256_text(canonical_json(payload))


def annotate_result(
    result: Mapping[str, Any],
    source_path: str | Path,
    *,
    processor: str,
    model_id: str,
    pipeline_version: str,
    config: Mapping[str, Any] | None = None,
    request_hash: str | None = None,
    caller_binding: Mapping[str, Any] | None = None,
    no_text_evidence: Mapping[str, Any] | bool | None = None,
    execution_status: str = "completed",
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach objective outcome metadata while preserving the legacy result.

    Empty blocks are deliberately conservative: they produce ``indeterminate``
    unless a processor supplies explicit, complete no-text evidence.  The
    service currently does not manufacture that evidence from a model's empty
    response.
    """

    annotated = dict(result)
    source = Path(source_path)
    source_info = _source_info(source)
    pages = _pages(result)
    coverage = _coverage(result, pages, source_info)
    observations = _observations(pages)
    text_blocks = _text_blocks(pages)
    quality_flags: list[str] = []
    uncertainty: list[str] = []

    if source_info["unsupported"]:
        quality_flags.append("unsupported")
    if source_info["zero_bytes"]:
        quality_flags.append("corrupt")
    if not source_info["available"]:
        quality_flags.append("source_unavailable")
    if coverage["status"] == "partial":
        quality_flags.append("partial_coverage")
    elif coverage["status"] == "unknown":
        uncertainty.append("coverage_unknown")

    low_confidence = any(
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and float(score) < LOW_CONFIDENCE_THRESHOLD
        for _text, score in text_blocks
        if score is not None
    )
    if low_confidence:
        quality_flags.append("low_confidence")
    if text_blocks and any(score is None for _text, score in text_blocks):
        quality_flags.append("confidence_unavailable")
        uncertainty.append("confidence_unavailable")

    normalized_execution = str(execution_status or "completed")
    if normalized_execution == "completed":
        if source_info["unsupported"]:
            normalized_execution = "unsupported"
        elif not source_info["available"]:
            normalized_execution = "failed"
        elif source_info["zero_bytes"] or (not source_info["readable"] and not text_blocks):
            normalized_execution = "corrupt"
    if normalized_execution not in EXECUTION_STATUSES:
        normalized_execution = "failed"
    if normalized_execution != "completed":
        quality_flags.append(normalized_execution)

    raw_hash = source_info["raw_sha256"]
    cfg_hash = config_sha256(config)
    resolved_request_hash = request_hash or derive_request_hash(
        source,
        processor=processor,
        model_id=model_id,
        pipeline_version=pipeline_version,
        config=config,
        caller_binding=caller_binding,
    )
    identity = {
        # PDF pages are image media for this contract; preserve the container
        # separately so audio/image consumers can share the same outcome enum.
        "media_kind": "image",
        "source_format": source_info["format"],
        "raw_sha256": raw_hash,
        "raw_size_bytes": source_info["size"],
        "source_size": source_info["size"],
        "processor": processor,
        "model_id": model_id,
        "pipeline_version": pipeline_version,
        "config_sha256": cfg_hash,
        "request_sha256": resolved_request_hash,
        "idempotency_key": resolved_request_hash,
        "caller_binding_sha256": caller_binding_sha256(caller_binding),
        "engine": result.get("engine_key"),
    }

    if no_text_evidence is None and not text_blocks:
        # An adapter may provide explicit detector telemetry.  Ordinary model
        # parsing output does not set this field; its empty blocks remain
        # indeterminate.  The independent uniform-pixel detector is the only
        # built-in producer today.
        no_text_evidence = result.get("no_text_evidence") or result.get("_no_text_evidence")
        if no_text_evidence is None:
            no_text_evidence = _uniform_image_evidence(source, source_info)
    explicit_negative = bool(no_text_evidence)
    source_valid_for_negative = (
        source_info["available"]
        and not source_info["zero_bytes"]
        and not source_info["unsupported"]
        and coverage["status"] == "complete"
        and normalized_execution == "completed"
        and not any(flag in quality_flags for flag in ("partial_coverage", "low_confidence", "corrupt"))
    )

    negative_evidence: dict[str, Any] | None = None
    if text_blocks:
        objective_outcome = "text_detected"
        reason = "nonempty_text_observed"
    elif explicit_negative and source_valid_for_negative:
        objective_outcome = "no_text_detected"
        reason = "verified_no_text_evidence"
        negative_payload: dict[str, Any] = {
            "schema": NEGATIVE_EVIDENCE_SCHEMA,
            "media_kind": "image",
            "source_format": source_info["format"],
            "raw_sha256": raw_hash,
            "raw_size_bytes": source_info["size"],
            "source_size": source_info["size"],
            "processor": processor,
            "model_id": model_id,
            "pipeline_version": pipeline_version,
            "config_sha256": cfg_hash,
            "request_sha256": resolved_request_hash,
            "coverage": coverage,
            "observations": observations,
            "exclusions": list(result.get("exclusions") or []),
            "thresholds": {"nonempty_text": "strip(text) != ''"},
            "quality_flags": [],
            "uncertainty": [],
            "method": dict(no_text_evidence) if isinstance(no_text_evidence, Mapping) else "processor_asserted",
        }
        artifact = canonical_json(negative_payload)
        negative_evidence = {
            "schema": NEGATIVE_EVIDENCE_SCHEMA,
            "artifact": artifact,
            "size_bytes": len(artifact.encode("utf-8")),
            # ``size`` is retained as a compatibility alias for early callers.
            "size": len(artifact.encode("utf-8")),
            "sha256": sha256_text(artifact),
        }
    else:
        objective_outcome = "indeterminate"
        if normalized_execution != "completed":
            reason = "execution_not_completed"
        elif not source_info["available"] or source_info["zero_bytes"]:
            reason = "source_invalid"
        elif coverage["status"] != "complete":
            reason = "coverage_incomplete"
        else:
            reason = "empty_observation_not_proof"
        quality_flags.append("empty_observation")
        uncertainty.append("model_empty_output_does_not_prove_no_text")

    if objective_outcome == "no_text_detected":
        # The negative evidence itself is the proof object; no empty-output
        # marker should be mistaken for a positive quality failure.
        quality_flags = [flag for flag in quality_flags if flag != "empty_observation"]

    if failure is not None:
        failure_payload: dict[str, Any] | None = dict(failure)
    elif normalized_execution != "completed":
        failure_payload = {
            "kind": normalized_execution,
            "message": "source or execution did not produce a qualified result",
        }
    else:
        failure_payload = None

    if low_confidence:
        quality_status = "low_confidence"
    elif objective_outcome in {"text_detected", "no_text_detected"} and not quality_flags:
        quality_status = "sufficient"
    else:
        quality_status = "unknown"
    quality = {
        "status": quality_status,
        "flags": sorted(set(quality_flags)),
        "thresholds": {
            "low_confidence": LOW_CONFIDENCE_THRESHOLD,
            "nonempty_text": "strip(text) != ''",
        },
    }
    objective: dict[str, Any] = {
        "schema": OBJECTIVE_SCHEMA,
        "media_kind": identity["media_kind"],
        "source_format": identity["source_format"],
        "raw_sha256": identity["raw_sha256"],
        "raw_size_bytes": identity["source_size"],
        "source_size": identity["source_size"],
        "processor": identity["processor"],
        "model_id": identity["model_id"],
        "pipeline_version": identity["pipeline_version"],
        "config_sha256": identity["config_sha256"],
        "request_sha256": identity["request_sha256"],
        "idempotency_key": identity["idempotency_key"],
        "objective_outcome": objective_outcome,
        "execution_status": normalized_execution,
        "reason": reason,
        "identity": identity,
        "coverage": coverage,
        "quality": quality,
        "uncertainty": sorted(set(uncertainty)),
        "failure": failure_payload,
        "observations": observations,
        "negative_evidence": negative_evidence,
        "execution": {"status": normalized_execution},
        "evidence": {
            "verification_status": "verified" if objective_outcome == "no_text_detected" else "not_persisted",
            "negative_evidence": negative_evidence,
        },
    }
    if caller_binding is not None:
        objective["caller_binding"] = dict(caller_binding)

    body_hash = sha256_text(canonical_json(objective))
    objective["artifact_sha256"] = body_hash
    objective["sha256"] = body_hash
    objective["size_bytes"] = len(canonical_json(objective).encode("utf-8"))

    annotated["objective_result"] = objective
    annotated["objective_outcome"] = objective_outcome
    annotated["execution_status"] = normalized_execution
    annotated["execution"] = {"status": normalized_execution}
    annotated["coverage"] = coverage
    annotated["quality"] = quality
    annotated["failure"] = failure_payload
    annotated["text_detection"] = {
        "schema": OBJECTIVE_SCHEMA,
        "status": objective_outcome,
        "reason": reason,
        "quality_flags": quality["flags"],
        "uncertainty": objective["uncertainty"],
        "coverage": coverage,
        "negative_evidence": negative_evidence,
    }
    if caller_binding is not None:
        annotated["caller_binding"] = dict(caller_binding)
    return annotated


def write_objective_sidecar(
    objective_result: Mapping[str, Any],
    source_path: str | Path,
    out_dir: str | Path,
    *,
    request_hash: str,
) -> tuple[Path, str]:
    """Write an idempotency-key-isolated sidecar and return path + file hash."""

    from .outputs import safe_output_stem

    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = safe_output_stem(Path(source_path))
    suffix = (request_hash or "unknown")[:32]
    path = output / f"{stem}.{suffix}.objective.json"
    payload = dict(objective_result)
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload_text, encoding="utf-8")
    return path, sha256_text(payload_text)


def validate_objective_sidecar(
    path: str | Path,
    *,
    request_hash: str,
    raw_sha256: str,
    source_size: int | None = None,
    profile_id: str,
    engine: str,
    config_sha256_value: str | None = None,
    expected_file_sha256: str | None = None,
) -> bool:
    """Validate a new sidecar before a job is reported as ``cache_hit``."""

    sidecar = Path(path)
    try:
        if sidecar.stat().st_size <= 0:
            return False
        raw = sidecar.read_bytes()
        if not raw:
            return False
        if expected_file_sha256 and hashlib.sha256(raw).hexdigest() != expected_file_sha256:
            return False
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("schema") != OBJECTIVE_SCHEMA:
        return False
    if payload.get("objective_outcome") not in TEXT_DETECTION_STATUSES:
        return False
    if payload.get("execution_status") not in EXECUTION_STATUSES:
        return False
    artifact_hash = payload.get("artifact_sha256")
    if not isinstance(artifact_hash, str):
        return False
    body = dict(payload)
    body.pop("artifact_sha256", None)
    body.pop("sha256", None)
    body.pop("size_bytes", None)
    if sha256_text(canonical_json(body)) != artifact_hash:
        return False
    if payload.get("sha256") != artifact_hash:
        return False
    if not isinstance(payload.get("size_bytes"), int) or payload["size_bytes"] <= 0:
        return False
    size_payload = dict(payload)
    size_payload.pop("size_bytes", None)
    if payload["size_bytes"] != len(canonical_json(size_payload).encode("utf-8")):
        return False
    identity = payload.get("identity")
    if not isinstance(identity, dict):
        return False
    if identity.get("request_sha256") != request_hash:
        return False
    if identity.get("idempotency_key") != request_hash:
        return False
    if identity.get("raw_sha256") != raw_sha256:
        return False
    if identity.get("media_kind") != "image":
        return False
    for key in (
        "raw_sha256",
        "raw_size_bytes",
        "source_size",
        "processor",
        "model_id",
        "pipeline_version",
        "config_sha256",
        "request_sha256",
        "idempotency_key",
    ):
        if payload.get(key) != identity.get(key):
            return False
    if source_size is not None and identity.get("source_size") != source_size:
        return False
    if identity.get("raw_size_bytes") != identity.get("source_size"):
        return False
    if identity.get("model_id") != profile_id:
        return False
    # Engine is included in the processor/model contract when available.  A
    # profile id is the stronger binding; accept legacy adapters without an
    # explicit engine field but reject an explicit mismatch.
    if identity.get("engine") not in (None, engine):
        return False
    if config_sha256_value and identity.get("config_sha256") != config_sha256_value:
        return False
    negative = payload.get("negative_evidence")
    if payload.get("objective_outcome") == "no_text_detected":
        execution = payload.get("execution")
        coverage = payload.get("coverage")
        quality = payload.get("quality")
        evidence = payload.get("evidence")
        if not isinstance(execution, dict) or execution.get("status") != "completed":
            return False
        if payload.get("execution_status") != "completed":
            return False
        if not isinstance(coverage, dict) or coverage.get("status") != "complete":
            return False
        if not isinstance(quality, dict) or quality.get("status") != "sufficient":
            return False
        if not isinstance(evidence, dict) or evidence.get("verification_status") != "verified":
            return False
        if evidence.get("negative_evidence") != negative:
            return False
        if not isinstance(negative, dict):
            return False
        artifact = negative.get("artifact")
        if not isinstance(artifact, str) or not artifact:
            return False
        if negative.get("size_bytes") != len(artifact.encode("utf-8")):
            return False
        if negative.get("sha256") != sha256_text(artifact):
            return False
        try:
            negative_payload = json.loads(artifact)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(negative_payload, dict):
            return False
        if negative_payload.get("schema") != NEGATIVE_EVIDENCE_SCHEMA:
            return False
        if negative_payload.get("raw_sha256") != identity.get("raw_sha256"):
            return False
        if negative_payload.get("source_size") != identity.get("source_size"):
            return False
        if negative_payload.get("raw_size_bytes") != identity.get("raw_size_bytes"):
            return False
        if negative_payload.get("request_sha256") != identity.get("request_sha256"):
            return False
        if negative_payload.get("config_sha256") != identity.get("config_sha256"):
            return False
    return True


def _source_info(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {
            "available": False,
            "size": None,
            "raw_sha256": None,
            "zero_bytes": False,
            "unsupported": False,
            "readable": False,
            "format": path.suffix.lower().lstrip("."),
        }
    suffix = path.suffix.lower()
    supported = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff", ".pdf"}
    readable = True
    if suffix in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}:
        readable = _image_readable(path)
    return {
        "available": True,
        "size": stat.st_size,
        "raw_sha256": file_sha256(path),
        "zero_bytes": stat.st_size == 0,
        "unsupported": suffix not in supported,
        "readable": readable,
        "format": suffix.lstrip("."),
    }


def _image_readable(path: Path) -> bool:
    try:
        import cv2  # type: ignore

        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return image is not None and image.size > 0
    except Exception:
        # A missing optional decoder must not turn an empty model response into
        # positive no-text evidence.  The result remains indeterminate.
        return True


def _uniform_image_evidence(path: Path, source_info: Mapping[str, Any]) -> dict[str, Any] | None:
    """Provide conservative no-text evidence for a genuinely near-uniform image.

    This is independent pixel telemetry, not an inference from an empty model
    response.  Complex or unreadable images deliberately return no evidence.
    """

    if source_info.get("format") == "pdf" or not source_info.get("readable"):
        return None
    try:
        import cv2  # type: ignore

        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None or image.size == 0:
            return None
        mean, stddev = cv2.meanStdDev(image)
        edges = cv2.Canny(image, 50, 150)
        edge_ratio = float(cv2.countNonZero(edges)) / float(image.size)
        pixel_stddev = float(stddev[0][0])
        thresholds = {"pixel_stddev_max": 1.5, "edge_ratio_max": 0.0005}
        if pixel_stddev > thresholds["pixel_stddev_max"] or edge_ratio > thresholds["edge_ratio_max"]:
            return None
        return {
            "method": "uniform-pixel-detector-v1",
            "thresholds": thresholds,
            "observed": {
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "pixel_stddev": round(pixel_stddev, 6),
                "edge_ratio": round(edge_ratio, 8),
            },
        }
    except Exception:
        return None


def _pages(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    pages = result.get("pages")
    if not isinstance(pages, list):
        return []
    return [page for page in pages if isinstance(page, dict)]


def _coverage(result: Mapping[str, Any], pages: list[dict[str, Any]], source_info: Mapping[str, Any]) -> dict[str, Any]:
    expected = result.get("expected_page_count")
    if not isinstance(expected, int) or expected < 1:
        expected = 1 if source_info.get("format") != "pdf" else None
    indices: list[int] = []
    for index, page in enumerate(pages):
        value = page.get("page_index", index)
        if isinstance(value, int) and value >= 0:
            indices.append(value)
    contiguous = indices == list(range(len(indices))) and bool(indices)
    if expected is not None and len(pages) != expected:
        status = "partial" if pages else "unknown"
    elif contiguous:
        status = "complete"
    elif pages:
        status = "partial"
    else:
        status = "unknown"
    return {
        "status": status,
        "pages_expected": expected,
        "pages_observed": len(pages),
        "page_indices": indices,
        "regions": [
            {
                "page_index": page.get("page_index", index),
                "scope": "full_page",
                "status": "observed",
            }
            for index, page in enumerate(pages)
        ],
        "exclusions": list(result.get("exclusions") or []),
    }


def _text_blocks(pages: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    blocks: list[tuple[str, Any]] = []
    for page in pages:
        raw_blocks = page.get("blocks")
        if not isinstance(raw_blocks, list):
            continue
        for block in raw_blocks:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text") or "").strip()
            if text:
                blocks.append((text, block.get("score")))
    return blocks


def _observations(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        raw_blocks = page.get("blocks")
        blocks = raw_blocks if isinstance(raw_blocks, list) else []
        nonempty = 0
        for block in blocks:
            if isinstance(block, dict) and str(block.get("text") or "").strip():
                nonempty += 1
        observations.append(
            {
                "page_index": page.get("page_index", index),
                "scope": "full_page",
                "block_count": len(blocks),
                "nonempty_text_block_count": nonempty,
            }
        )
    return observations
