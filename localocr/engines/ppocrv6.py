from __future__ import annotations

from typing import Any

from .common import combine_predictions, recognized_lines

from paddleocr import PaddleOCR

MODEL_NAME = "PP-OCRv6_medium (det + rec)"
ENGINE_NAME = "PP-OCRv6_medium"
PIPELINE_VERSION = "PP-OCRv6"
COORDINATE_SPACE = "image_pixels"
DEFAULT_OPTIONS: dict[str, Any] = {
    "ocr_version": PIPELINE_VERSION,
    "lang": "ch",
    "use_doc_orientation_classify": True,
    "use_doc_unwarping": False,
    "use_textline_orientation": True,
}


class PPOCRv6Engine:
    """PP-OCRv6 detection and recognition; do not deform flat screenshots by default."""

    def __init__(
        self,
        device: str = "gpu:0",
        *,
        profile_id: str = "ppocrv6-medium",
        model_name: str = MODEL_NAME,
        engine_name: str = ENGINE_NAME,
        pipeline_version: str = PIPELINE_VERSION,
        options: dict[str, Any] | None = None,
    ):
        self.device = device
        self.profile_id = profile_id
        self._model_name = model_name
        self.engine_name = engine_name
        self.pipeline_version = pipeline_version
        self.options = dict(DEFAULT_OPTIONS)
        if options:
            self.options.update(options)
        self.options.setdefault("ocr_version", self.pipeline_version)
        self._ocr: PaddleOCR | None = None

    def _ensure(self):
        if self._ocr is None:
            params = dict(self.options)
            params["device"] = self.device
            self._ocr = PaddleOCR(**params)
        return self._ocr

    @property
    def model_name(self) -> str:
        return self._model_name

    def predict_image(self, image_path: str) -> dict[str, Any]:
        return combine_predictions(self._ensure().predict(image_path), self._convert_result, options=self.options)

    def _convert_result(self, data: dict[str, Any]) -> dict[str, Any]:
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, dict) else None
        blocks = [{"type": "text", **row} for row in recognized_lines(data)]
        return {
            "engine": self.engine_name,
            "model": self.model_name,
            "model_id": self.profile_id,
            "device": self.device,
            "page_angle": angle,
            "page_width": data.get("width"),
            "page_height": data.get("height"),
            "pages": [{
                "page_index": 0,
                "blocks": blocks,
                "coordinate_space": COORDINATE_SPACE,
                "width": data.get("width"),
                "height": data.get("height"),
            }],
        }


def _norm_poly(poly):
    if poly is None:
        return None
    return [[int(round(float(p[0]))), int(round(float(p[1])))] for p in poly]


def _norm_box(box):
    if box is None:
        return None
    return [int(round(float(v))) for v in box]


def _rect_from_geometry(geometry):
    if not geometry:
        return None
    if len(geometry) == 4 and all(not isinstance(value, list) for value in geometry):
        return [int(round(float(value))) for value in geometry]
    points = [point for point in geometry if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [int(round(min(xs))), int(round(min(ys))), int(round(max(xs))), int(round(max(ys)))]
