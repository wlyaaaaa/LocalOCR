"""Reproducible cache identity; large artifacts are hashed only after file changes."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import sys
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=4096)
def _content_hash(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    stat = Path(path).stat()
    if (stat.st_size, stat.st_mtime_ns) != (size, mtime_ns):
        raise ValueError("Model or implementation file changed during fingerprinting")
    return digest.hexdigest()


def file_identity(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "sha256": _content_hash(str(path), stat.st_size, stat.st_mtime_ns)}


@lru_cache(maxsize=64)
def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def execution_identity(profile) -> dict:
    root = Path(__file__).parent
    adapter = profile.adapter.split(":", 1)[0]
    implementation = {}
    paths = sorted(root.glob("*.py")) + [root / "engines/common.py", root.parent / (adapter.replace(".", "/") + ".py")]
    for path in paths:
        if path.is_file():
            implementation[str(path.relative_to(root.parent))] = file_identity(path)
    packages = ("paddlepaddle-gpu", "paddleocr", "paddlex", "numpy", "pypdfium2", "pillow")
    if profile.runtime_backend == "torch":
        packages = ("torch", "transformers", "pillow", "tokenizers")
    artifacts = {}
    for pattern in profile.artifact_paths:
        expanded = os.path.expandvars(os.path.expanduser(pattern))
        import glob
        matches = sorted(glob.glob(expanded))
        if not matches:
            artifacts[pattern] = {"status": "not_installed"}
        for raw in matches:
            target = Path(raw)
            files = sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
            for path in files:
                if path.name.startswith(".") or path.suffix in {".lock", ".log", ".md", ".pyc"} or path.name in {"LICENSE"}:
                    continue
                artifacts[str(path)] = file_identity(path)
    descriptor = {"schema": "localocr.execution-identity.v1", "profile": asdict(profile),
                  "python": list(sys.version_info[:2]),
                  "packages": {name: _package_version(name) for name in packages},
                  "implementation": implementation, "artifacts": artifacts}
    encoded = json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(encoded.encode()).hexdigest(), **descriptor}


def execution_sha256(profile) -> str:
    return execution_identity(profile)["sha256"]
