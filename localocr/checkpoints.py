"""Request-local atomic page checkpoints, removed with the request scratch directory."""
from __future__ import annotations
import json
import os
from pathlib import Path


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
    os.replace(temporary, path)


def binding(payload: dict) -> dict:
    return {key: payload.get(key) for key in ("profile_id", "profile_revision", "source_sha256", "path")}


def save_header(directory: Path, result: dict, payload: dict) -> None:
    _atomic(directory / "header.json", {**result, "pages": [], "checkpoint_binding": binding(payload)})


def save_page(directory: Path, page: dict) -> None:
    _atomic(directory / f"page_{page['page_index']:06d}.json", page)


def read_partial(directory: Path, payload: dict) -> dict | None:
    try:
        header = json.loads((directory / "header.json").read_text(encoding="utf-8"))
        if header.pop("checkpoint_binding", None) != binding(payload):
            return None
        count = header.get("expected_page_count")
        if type(count) is not int or count < 1:
            return None
        pages = []
        for path in sorted(directory.glob("page_*.json")):
            page = json.loads(path.read_text(encoding="utf-8"))
            index = page.get("page_index")
            if type(index) is not int or not 0 <= index < count or path.name != f"page_{index:06d}.json":
                return None
            pages.append(page)
        return {**header, "pages": pages} if pages else None
    except (OSError, ValueError, TypeError):
        return None
