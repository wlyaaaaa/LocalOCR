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
    """Compatibility wrapper returning Smart Router v2's effective engine."""
    from .smart_router import choose_smart_route

    return choose_smart_route(path, engine_choice=override).effective_engine


def collect_input_inventory(inputs: list[str], recursive: bool) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    skipped: list[dict[str, str]] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(p for p in path.glob("**/*" if recursive else "*") if p.is_file())
        else:
            skipped.append({"path": str(path), "status": "missing"})
            continue
        for candidate in candidates:
            if is_supported(candidate):
                files.append(candidate)
            else:
                skipped.append({"path": str(candidate), "status": "unsupported"})
    return sorted(set(files), key=lambda p: str(p).lower()), skipped


def collect_files(inputs: list[str], recursive: bool) -> list[Path]:
    return collect_input_inventory(inputs, recursive)[0]
