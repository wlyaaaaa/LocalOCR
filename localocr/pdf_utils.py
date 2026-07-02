from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium


def render_pdf_to_images(pdf_path: Path, scale: float = 2.0) -> list[bytes]:
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


def render_pdf_to_files(pdf_path: Path, out_dir: Path, scale: float = 2.0) -> list[Path]:
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
