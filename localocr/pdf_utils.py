from __future__ import annotations

import io
import struct
from pathlib import Path

import pypdfium2 as pdfium


DEFAULT_RENDER_SCALE = 2.0


def render_pdf_to_images(pdf_path: Path, scale: float = DEFAULT_RENDER_SCALE) -> list[bytes]:
    """把 PDF 每页渲染为 PNG 字节列表（内存中）。scale=2.0 约 200dpi，兼顾清晰度与速度。"""
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages: list[bytes] = []
    n = len(pdf)
    for i in range(n):
        page = pdf[i]
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        pages.append(buf.getvalue())
    pdf.close()
    return pages


def render_pdf_to_files(
    pdf_path: Path,
    out_dir: Path,
    scale: float = DEFAULT_RENDER_SCALE,
) -> list[Path]:
    """把 PDF 每页渲染为 PNG 文件，返回路径列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(str(pdf_path))
    paths: list[Path] = []
    n = len(pdf)
    for i in range(n):
        page = pdf[i]
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        out = out_dir / f"{pdf_path.stem}_p{i + 1:03d}.png"
        pil.save(out, format="PNG")
        paths.append(out)
    pdf.close()
    return paths


def read_png_dimensions(image_path: Path) -> tuple[int, int] | None:
    """Read dimensions from a rendered PNG without loading image pixels."""

    try:
        with Path(image_path).open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def rendered_pdf_page_metadata(
    image_path: Path,
    *,
    scale: float = DEFAULT_RENDER_SCALE,
) -> dict[str, object]:
    """Describe the pixel coordinate frame used for a rendered PDF page."""

    size = read_png_dimensions(image_path)
    width = size[0] if size else None
    height = size[1] if size else None
    return {
        "coordinate_space": "image_pixels",
        "rendered_pdf_pixels": True,
        "render_scale": float(scale),
        "rendered_width": width,
        "rendered_height": height,
        "rendered_pdf_size": {"width": width, "height": height},
    }
