from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write(path: Path, payload: bytes) -> None:
    """Publish a whole artifact or nothing; a job manifest is the final commit point."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_outputs(
    result: dict[str, Any], file_path: Path, out_dir: Path
) -> dict[str, Path]:
    """把单个文件的结果写成 .txt / .md / .json 三份，返回各路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_output_stem(file_path)
    txt_path = out_dir / f"{stem}.txt"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    atomic_write(txt_path, _to_txt(result, file_path).encode("utf-8"))
    atomic_write(md_path, _to_md(result, file_path).encode("utf-8"))
    atomic_write(json_path, _to_json(result, file_path).encode("utf-8"))
    return {"txt": txt_path, "md": md_path, "json": json_path}


def write_isolated_projections(
    result: dict[str, Any],
    file_path: Path,
    out_dir: Path,
    *,
    request_hash: str,
) -> dict[str, Path]:
    """Write hash-isolated projections while retaining legacy stem outputs.

    The legacy ``<stem>.txt|md|json`` files remain convenient display
    projections and are intentionally compatible.  Cacheable artifacts use
    these request-bound paths so same-stem files from different directories
    cannot share a canonical output path.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = (request_hash or "unknown")[:32]
    stem = safe_output_stem(file_path)
    paths = {
        "canonical_txt": out_dir / f"{stem}.{suffix}.txt",
        "canonical_md": out_dir / f"{stem}.{suffix}.md",
        "canonical_json": out_dir / f"{stem}.{suffix}.json",
    }
    atomic_write(paths["canonical_txt"], _to_txt(result, file_path).encode("utf-8"))
    atomic_write(paths["canonical_md"], _to_md(result, file_path).encode("utf-8"))
    atomic_write(paths["canonical_json"], _to_json(result, file_path).encode("utf-8"))
    return paths


def safe_output_stem(path: Path) -> str:
    import re

    return re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", path.stem)[:120]


def _blocks_text(pages: list[dict]) -> list[str]:
    lines: list[str] = []
    for page in pages:
        for b in page.get("blocks", []):
            t = (b.get("text") or "").strip()
            if t:
                lines.append(t)
    return lines


def _to_txt(result: dict, file_path: Path) -> str:
    parts = [
        f"文件: {file_path.name}",
        f"引擎: {result.get('engine')}",
        f"模型: {result.get('model')}",
        f"设备: {result.get('device')}",
        "",
    ]
    for page in result.get("pages", []):
        idx = page.get("page_index", 0)
        parts.append(f"----- 第 {idx + 1} 页 -----")
        for b in page.get("blocks", []):
            t = (b.get("text") or "").strip()
            if t:
                if b.get("type") == "table":
                    parts.append(t)
                elif b.get("type") == "formula":
                    parts.append(t)
                else:
                    parts.append(t)
        parts.append("")
    return "\n".join(parts)


def _to_md(result: dict, file_path: Path) -> str:
    parts = [
        f"# {file_path.name}\n",
        f"- 引擎: `{result.get('engine')}`  模型: `{result.get('model')}`  设备: `{result.get('device')}`\n",
    ]
    if result.get("page_angle") is not None:
        parts.append(f"- 方向检测角度: {result['page_angle']}\n")
    parts.append("\n")
    pages = result.get("pages", [])
    for page in pages:
        idx = page.get("page_index", 0)
        parts.append(f"## 第 {idx + 1} 页\n\n")
        for b in page.get("blocks", []):
            t = (b.get("text") or "").strip()
            if not t:
                continue
            btype = b.get("type", "text")
            if btype == "table":
                parts.append(f"{t}\n\n")
            elif btype == "formula":
                parts.append(f"$$\n{t}\n$$\n\n")
            elif btype in ("title", "heading"):
                parts.append(f"### {t}\n\n")
            else:
                parts.append(f"{t}\n\n")
    return "".join(parts)


def _to_json(result: dict, file_path: Path) -> str:
    payload = {
        "file": str(file_path),
        "file_name": file_path.name,
        "engine": result.get("engine"),
        "engine_key": result.get("engine_key"),
        "model": result.get("model"),
        "model_id": result.get("model_id"),
        "device": result.get("device"),
        "page_angle": result.get("page_angle"),
        "page_width": result.get("page_width"),
        "page_height": result.get("page_height"),
        "route": result.get("route"),
        # New objective-result fields are additive.  Existing consumers can
        # continue reading engine/pages/route while callers that need to
        # distinguish empty OCR from no-text evidence use these fields.
        "objective_result": result.get("objective_result"),
        "objective_outcome": result.get("objective_outcome"),
        "execution_status": result.get("execution_status"),
        "execution": result.get("execution"),
        "coverage": result.get("coverage"),
        "quality": result.get("quality"),
        "failure": result.get("failure"),
        "text_detection": result.get("text_detection"),
        "objective_result_file": result.get("objective_result_file"),
        "objective_result_sha256": result.get("objective_result_sha256"),
        "output_file_size_bytes": result.get("output_file_size_bytes"),
        "caller_binding": result.get("caller_binding"),
        "pages": result.get("pages", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
