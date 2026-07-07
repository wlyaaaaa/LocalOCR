from __future__ import annotations

from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
INPUT_EXTS = IMAGE_EXTS | PDF_EXTS


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def is_pdf(path: Path) -> bool:
    return path.suffix.lower() in PDF_EXTS


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in INPUT_EXTS


def route_engine(path: Path, override: str = "auto") -> str:
    """返回 engine 族：ocr、vl 或 structure。

    - auto：图片→ocr；PDF→vl（见设计文档 §4 路由规则）。
    - ocr/vl/structure：强制覆盖。
    """
    if override not in ("auto", "ocr", "vl", "structure"):
        raise ValueError(f"engine 必须是 auto/ocr/vl/structure，得到 {override!r}")
    if override != "auto":
        return override
    if is_pdf(path):
        return "vl"
    return "ocr"


def collect_files(inputs: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_file():
            if is_supported(p):
                files.append(p)
            else:
                print(f"[WARN] 不支持的文件类型，跳过：{p}")
        elif p.is_dir():
            pattern = "**/*" if recursive else "*"
            for child in sorted(p.glob(pattern)):
                if child.is_file() and is_supported(child):
                    files.append(child)
        else:
            print(f"[WARN] 不存在：{p}")
    return sorted(set(files), key=lambda x: str(x).lower())
