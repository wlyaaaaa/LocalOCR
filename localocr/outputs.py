from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_outputs(result: dict[str, Any], file_path: Path, out_dir: Path) -> dict[str, Path]:
    """把单个文件的结果写成 .txt / .md / .json 三份，返回各路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_output_stem(file_path)
    txt_path = out_dir / f"{stem}.txt"
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    txt_path.write_text(_to_txt(result, file_path), encoding="utf-8")
    md_path.write_text(_to_md(result, file_path), encoding="utf-8")
    json_path.write_text(_to_json(result, file_path), encoding="utf-8")
    return {"txt": txt_path, "md": md_path, "json": json_path}


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
    parts = [f"文件: {file_path.name}", f"引擎: {result.get('engine')}",
             f"模型: {result.get('model')}", f"设备: {result.get('device')}", ""]
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
        "model": result.get("model"),
        "device": result.get("device"),
        "page_angle": result.get("page_angle"),
        "pages": result.get("pages", []),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
