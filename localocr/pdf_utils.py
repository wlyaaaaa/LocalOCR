from __future__ import annotations

import io
import struct
from pathlib import Path

import pypdfium2 as pdfium


DEFAULT_RENDER_SCALE = 2.0


def render_pdf_to_images(pdf_path: Path, scale: float = DEFAULT_RENDER_SCALE) -> list[bytes]:
    """Explicit legacy materialization; normal inference uses iter_input_pages."""
    pages = []
    with pdfium.PdfDocument(str(pdf_path)) as document:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        with io.BytesIO() as buffer:
                            image.save(buffer, format="PNG")
                            pages.append(buffer.getvalue())
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
    return pages


def render_pdf_to_files(pdf_path: Path, out_dir: Path, scale: float = DEFAULT_RENDER_SCALE) -> list[Path]:
    """Compatibility API; explicitly requested files remain the caller's responsibility."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with pdfium.PdfDocument(str(pdf_path)) as document:
        for index in range(len(document)):
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    image = bitmap.to_pil()
                    try:
                        path = out_dir / f"{pdf_path.stem}_p{index + 1:03d}.png"
                        image.save(path, format="PNG")
                        paths.append(path)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                page.close()
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


def input_page_count(path: Path) -> int:
    """Count input pages/frames independently of model output."""
    if path.suffix.casefold() == ".pdf":
        with pdfium.PdfDocument(str(path)) as document:
            return len(document)
    from PIL import Image
    with Image.open(path) as image:
        return int(getattr(image, "n_frames", 1))


def iter_input_pages(path: Path, out_dir: Path, *, indices=None, dpi: float = 216,
                     max_pixels: int = 24_000_000):
    """Render at most one bounded page/frame at a time and remove it on advance."""
    from PIL import Image
    import math
    if not math.isfinite(dpi) or not 72 <= dpi <= 600:
        raise ValueError("PDF DPI must be finite and between 72 and 600")
    if max_pixels < 1:
        raise ValueError("max_pixels must be positive")
    out_dir.mkdir(parents=True, exist_ok=True)
    count = input_page_count(path)
    selected = list(range(count)) if indices is None else list(indices)
    if len(selected) != len(set(selected)) or any(type(i) is not int or i < 0 or i >= count for i in selected):
        raise ValueError("Page indices must be unique zero-based indices of the input")
    if path.suffix.casefold() == ".pdf":
        with pdfium.PdfDocument(str(path)) as document:
            for index in selected:
                target = out_dir / f"page_{index:06d}.png"
                page = document[index]
                try:
                    width, height = page.get_size()
                    scale = min(dpi / 72.0, math.sqrt(max_pixels / max(1, width * height)))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            image.save(target, format="PNG")
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                metadata = rendered_pdf_page_metadata(target, scale=scale)
                metadata.update(source_page_index=index, render_dpi=72.0 * scale,
                                source_coordinate_mapping_status="render_parameters_only")
                try:
                    yield index, target, metadata
                finally:
                    target.unlink(missing_ok=True)
    else:
        with Image.open(path) as image:
            for index in selected:
                image.seek(index)
                if count == 1:
                    yield index, path, {"source_frame_index": index, "source_frame_count": count}
                    continue
                target = out_dir / f"frame_{index:06d}.png"
                converted = image.convert("RGB")
                try:
                    converted.save(target, format="PNG")
                finally:
                    converted.close()
                try:
                    yield index, target, {"source_frame_index": index, "source_frame_count": count}
                finally:
                    target.unlink(missing_ok=True)


def uniform_page_hint(path: Path) -> bool:
    """Routing hint only, never an objective no-text certificate."""
    from PIL import Image
    with Image.open(path) as image:
        gray = image.convert("L")
        try:
            low, high = gray.getextrema()
            return high - low <= 1
        finally:
            gray.close()
