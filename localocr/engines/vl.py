from __future__ import annotations

import re
from typing import Any

from .common import combine_predictions, optional_score

from paddleocr import PaddleOCRVL

MODEL_NAME = "PaddleOCR-VL-1.6"
ENGINE_NAME = "PaddleOCR-VL-1.6"
PIPELINE_VERSION = "v1.6"
COORDINATE_SPACE = "image_pixels"
_NON_TEXT_LABELS = {
    "face",
    "person",
    "human",
    "portrait",
    "figure",
    "image",
    "人脸",
    "人物",
    "人像",
}
DEFAULT_OPTIONS: dict[str, Any] = {
    "pipeline_version": PIPELINE_VERSION,
    "vl_rec_backend": "native",
    "use_queues": False,
    "use_doc_orientation_classify": True,
    "use_doc_unwarping": True,
}


class VLEngine:
    """PaddleOCR-VL-1.6 引擎，用于 PDF/合同/论文/表格/公式/多栏复杂文档（需求 3）。"""

    def __init__(
        self,
        device: str = "gpu:0",
        *,
        profile_id: str = "paddleocr-vl-1.6",
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
        self.options.setdefault("pipeline_version", self.pipeline_version)
        self._vl: PaddleOCRVL | None = None

    def _ensure(self):
        if self._vl is None:
            params = dict(self.options)
            params["device"] = self.device
            self._vl = PaddleOCRVL(**params)
        return self._vl

    @property
    def model_name(self) -> str:
        return self._model_name

    def predict_image(self, image_path: str) -> dict[str, Any]:
        return combine_predictions(self._ensure().predict(image_path), self._convert_result, options=self.options)

    def _convert_result(self, data: dict[str, Any]) -> dict[str, Any]:
        parsing = data.get("parsing_res_list") or []
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, dict) else None
        blocks = []
        excluded_regions = []
        for b in parsing:
            label = str(b.get("block_label", "text"))
            bbox = _norm_box(b.get("block_bbox"))
            polygon = _norm_poly(b.get("block_polygon_points"))
            score = _optional_score(
                b.get("score", b.get("block_score", b.get("confidence")))
            )
            if _is_non_text_label(label):
                excluded_regions.append({
                    "type": label,
                    "label": label,
                    "bbox": bbox,
                    "rect": _rect_from_geometry(bbox or polygon),
                    "polygon": polygon,
                    "order": b.get("block_order"),
                    "block_id": b.get("block_id"),
                    "group_id": b.get("group_id"),
                    "coordinate_space": COORDINATE_SPACE,
                })
                continue
            blocks.append({
                "type": label,
                "text": str(b.get("block_content", "")),
                "score": score,
                "bbox": bbox,
                "rect": _rect_from_geometry(bbox or polygon),
                "polygon": polygon,
                "order": b.get("block_order"),
                "block_id": b.get("block_id"),
                "group_id": b.get("group_id"),
                "coordinate_space": COORDINATE_SPACE,
            })
        blocks.sort(key=lambda x: (x.get("order") is None, x.get("order") or 0))
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
                "excluded_regions": excluded_regions,
                "coordinate_space": COORDINATE_SPACE,
                "width": data.get("width"),
                "height": data.get("height"),
            }],
        }


def _norm_box(box):
    if box is None:
        return None
    return [int(round(float(v))) for v in box]


def _norm_poly(poly):
    if poly is None:
        return None
    return [[float(p[0]), float(p[1])] for p in poly]


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


def _optional_score(value):
    return optional_score(value)


def _is_non_text_label(label: str) -> bool:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", label.casefold())
    return bool(set(normalized.split()) & _NON_TEXT_LABELS)
