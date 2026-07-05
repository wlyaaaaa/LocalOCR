from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .engines import get_engine
from .gpu_probe import format_probe, probe_gpu
from .outputs import write_outputs
from .pdf_utils import render_pdf_to_files
from .router import collect_files, is_pdf, route_engine


class OCRService:
    """Long-lived OCR runtime that keeps Paddle models warm in memory."""

    def __init__(
        self,
        *,
        device: str = "gpu:0",
        tmp_dir: str | Path = "_pdf_pages/api",
        probe_on_start: bool = True,
    ) -> None:
        self.device = device
        self.tmp_dir = Path(tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._engine_cache: dict[str, Any] = {}
        self._lock = threading.RLock()
        self.gpu_info = probe_gpu() if probe_on_start else None

    @property
    def gpu_summary(self) -> str | None:
        if self.gpu_info is None:
            return None
        return format_probe(self.gpu_info)

    @property
    def loaded_engines(self) -> list[str]:
        return sorted(self._engine_cache)

    def _engine(self, key: str):
        if key not in self._engine_cache:
            self._engine_cache[key] = get_engine(key, device=self.device)
        return self._engine_cache[key]

    def _ocr_pdf_with_engine(self, pdf_path: Path, engine_key: str, engine) -> dict[str, Any]:
        images = render_pdf_to_files(pdf_path, out_dir=self.tmp_dir)
        pages: list[dict[str, Any]] = []
        for i, img in enumerate(images):
            result = engine.predict_image(str(img))
            for page in result.get("pages", []):
                page["page_index"] = i
                pages.append(page)
        if engine_key == "vl":
            return {
                "engine": "PaddleOCR-VL-1.6",
                "model": engine.model_name,
                "device": engine.device,
                "pages": pages,
            }
        return {
            "engine": "PP-OCRv6_medium",
            "model": engine.model_name,
            "device": engine.device,
            "pages": pages,
        }

    def process_file(self, path: Path, engine_choice: str = "auto") -> dict[str, Any]:
        engine_key = route_engine(path, engine_choice)
        engine = self._engine(engine_key)
        if is_pdf(path):
            result = self._ocr_pdf_with_engine(path, engine_key, engine)
        else:
            result = engine.predict_image(str(path))
        result["source_file"] = str(path)
        result["engine_key"] = engine_key
        return result

    def process_inputs(
        self,
        inputs: list[str | Path],
        *,
        engine_choice: str = "auto",
        recursive: bool = False,
        out_dir: str | Path | None = None,
        write_files: bool = True,
    ) -> dict[str, Any]:
        files = collect_files([str(p) for p in inputs], recursive)
        if not files:
            raise FileNotFoundError("未找到可识别文件，支持 png/jpg/jpeg/bmp/webp/tif/tiff/pdf。")

        output_dir = Path(out_dir) if out_dir is not None else Path("outputs/api")
        results: list[dict[str, Any]] = []
        with self._lock:
            for file_path in files:
                result = self.process_file(file_path, engine_choice)
                if write_files:
                    paths = write_outputs(result, file_path, output_dir)
                    result["output_files"] = {k: str(v) for k, v in paths.items()}
                results.append(result)
        return {
            "ok": True,
            "count": len(results),
            "device": self.device,
            "gpu": self.gpu_summary,
            "loaded_engines": self.loaded_engines,
            "results": results,
        }
