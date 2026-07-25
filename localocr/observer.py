from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LIST_SCHEMA = "local-ai-observer.jobs.v1"
DETAIL_SCHEMA = "local-ai-observer.job.v1"
SERVICE = "localocr"
OBSERVER_MANIFEST_LIMIT_BYTES = 8 * 1024 * 1024
_JOB_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_STATE_STAGE = {
    "running": ("running", "processing"),
    "completed": ("completed", "completed"),
    "failed": ("failed", "failed"),
}


class ObserverProjection:
    """Read-only, privacy-safe projection of LocalOCR job manifests."""

    def __init__(
        self,
        job_dir: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.job_dir = Path(job_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def list_jobs(self, *, limit: int = 100) -> dict[str, Any]:
        observed_at = _as_utc(self._now())
        projected: list[dict[str, Any]] = []
        for manifest in self._read_manifests():
            job = _project_job(manifest, observed_at)
            if job is not None:
                projected.append(job)
        projected.sort(
            key=lambda job: (
                job["timing"]["updated_utc"] or "",
                job["timing"]["started_utc"] or "",
                job["job_id"],
            ),
            reverse=True,
        )
        bounded_limit = max(1, min(int(limit), 200))
        return {
            "schema": LIST_SCHEMA,
            "service": SERVICE,
            "observed_utc": _format_utc(observed_at),
            "jobs": projected[:bounded_limit],
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            return None
        observed_at = _as_utc(self._now())
        for manifest in self._read_manifests():
            if manifest.get("job_id") != job_id:
                continue
            job = _project_job(manifest, observed_at)
            if job is None:
                return None
            return {
                "schema": DETAIL_SCHEMA,
                "service": SERVICE,
                "observed_utc": _format_utc(observed_at),
                "job": job,
            }
        return None

    def _read_manifests(self) -> list[dict[str, Any]]:
        try:
            paths = list(self.job_dir.glob("*.json"))
        except OSError:
            return []
        manifests: list[dict[str, Any]] = []
        for path in paths:
            try:
                with path.open("rb") as stream:
                    encoded = stream.read(OBSERVER_MANIFEST_LIMIT_BYTES + 1)
                if len(encoded) > OBSERVER_MANIFEST_LIMIT_BYTES:
                    continue
                value = json.loads(encoded)
            except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                manifests.append(value)
        return manifests


def _project_job(manifest: dict[str, Any], observed_at: datetime) -> dict[str, Any] | None:
    job_id = manifest.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
        return None

    raw_state = manifest.get("status")
    state, stage = (
        _STATE_STAGE.get(raw_state, ("unknown", "unknown"))
        if isinstance(raw_state, str)
        else ("unknown", "unknown")
    )
    started_at = _parse_utc(manifest.get("started_at"))
    updated_at = _parse_utc(manifest.get("updated_at"))
    elapsed_ms = _elapsed_ms(state, started_at, updated_at, observed_at)

    return {
        "job_id": job_id,
        "state": state,
        "stage": stage,
        "mode": _safe_identifier(manifest.get("engine")),
        "model": _safe_identifier(manifest.get("model_id")),
        "progress": {
            "status": "unavailable",
            "completed": None,
            "total": None,
            "unit": None,
        },
        "timing": {
            "status": "available" if elapsed_ms is not None else "unavailable",
            "started_utc": _format_utc(started_at) if started_at is not None else None,
            "updated_utc": _format_utc(updated_at) if updated_at is not None else None,
            "elapsed_ms": elapsed_ms,
        },
        "tokens": {
            "status": "not_applicable",
            "input": None,
            "output": None,
            "tps": None,
        },
    }


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, str) and _IDENTIFIER_PATTERN.fullmatch(value):
        return value
    return None


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("observer clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _elapsed_ms(
    state: str,
    started_at: datetime | None,
    updated_at: datetime | None,
    observed_at: datetime,
) -> int | None:
    if started_at is None:
        return None
    endpoint = observed_at if state == "running" else updated_at
    if endpoint is None:
        return None
    return max(0, int((endpoint - started_at).total_seconds() * 1000))
