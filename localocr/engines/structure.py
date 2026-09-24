from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any

from .common import combine_predictions, recognized_lines

from paddleocr import PPStructureV3

MODEL_NAME = "PP-StructureV3 + PP-OCRv5"
ENGINE_NAME = "PP-StructureV3"
PIPELINE_VERSION = "PP-StructureV3"
DEFAULT_OPTIONS: dict[str, Any] = {
    "lang": "ch",
    "ocr_version": "PP-OCRv5",
    "use_doc_orientation_classify": True,
    "use_doc_unwarping": True,
    "use_textline_orientation": True,
    "use_table_recognition": True,
    "use_formula_recognition": True,
    "use_chart_recognition": False,
    "use_seal_recognition": True,
    "use_region_detection": True,
    "format_block_content": True,
}

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
_STRUCTURE_DETAIL_KEYS = {
    "table_res_list",
    "formula_res_list",
    "seal_res_list",
    "region_det_res",
    "region_det_res_list",
    "region_res_list",
}


class StructureV3Engine:
    """PP-StructureV3 adapter for layout/table/formula-oriented OCR."""

    def __init__(
        self,
        device: str = "gpu:0",
        *,
        profile_id: str = "pp-structure-v3",
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
        self._structure: PPStructureV3 | None = None

    def _ensure(self):
        if self._structure is None:
            params = dict(self.options)
            params["device"] = self.device
            self._structure = PPStructureV3(**params)
        return self._structure

    @property
    def model_name(self) -> str:
        return self._model_name

    def predict_image(self, image_path: str) -> dict[str, Any]:
        return combine_predictions(self._ensure().predict(image_path), self._convert_result, options=self.options)

    def _convert_result(self, data: dict[str, Any]) -> dict[str, Any]:
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, dict) else None

        excluded_regions: list[dict[str, Any]] = []
        parsing = data.get("parsing_res_list") or []
        blocks = _blocks_from_parsing(
            parsing,
            excluded_regions=excluded_regions,
        )
        if not parsing:
            blocks = _blocks_from_overall_ocr(data.get("overall_ocr_res") or {})

        text_lines = _text_lines_from_overall_ocr(data.get("overall_ocr_res") or {})
        structure_details = _structure_details(data)

        page: dict[str, Any] = {
            "page_index": int(data.get("page_index") or 0),
            "blocks": blocks,
            "structure_keys": sorted(str(k) for k in data.keys()),
            "structure_details": structure_details,
            "text_lines": text_lines,
            "excluded_regions": excluded_regions,
            "coordinate_space": COORDINATE_SPACE,
        }
        if data.get("width") is not None:
            page["width"] = data.get("width")
        if data.get("height") is not None:
            page["height"] = data.get("height")
        table_count = len(data.get("table_res_list") or [])
        if table_count:
            page["table_count"] = table_count

        return {
            "engine": self.engine_name,
            "model": self.model_name,
            "model_id": self.profile_id,
            "device": self.device,
            "page_angle": angle,
            "page_width": data.get("width"),
            "page_height": data.get("height"),
            "pages": [page],
        }


def _blocks_from_parsing(
    parsing: list[dict[str, Any]],
    *,
    excluded_regions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for raw in parsing:
        label = str(raw.get("block_label") or "text")
        btype = _block_type(label)
        content = str(raw.get("block_content") or "")
        text = content if btype in {"table", "formula"} else _strip_html(content)
        score = _optional_score(
            raw.get("score", raw.get("block_score", raw.get("confidence")))
        )
        bbox, polygon, rect = _geometry_fields(
            raw.get("block_bbox"),
            raw.get("block_polygon_points"),
        )
        if _is_non_text_label(label, btype):
            if excluded_regions is not None:
                excluded_regions.append(
                    {
                        "type": btype,
                        "label": label,
                        "bbox": bbox,
                        "rect": rect,
                        "polygon": polygon,
                        "order": raw.get("block_order"),
                        "block_id": raw.get("block_id"),
                        "group_id": raw.get("group_id"),
                        "coordinate_space": COORDINATE_SPACE,
                    }
                )
            continue
        blocks.append(
            {
                "type": btype,
                "label": label,
                "text": text.strip(),
                "score": score,
                "bbox": bbox,
                "rect": rect,
                "polygon": polygon,
                "order": raw.get("block_order"),
                "block_id": raw.get("block_id"),
                "group_id": raw.get("group_id"),
                "coordinate_space": COORDINATE_SPACE,
            }
        )
    blocks.sort(key=lambda x: (_sort_order(x), _sort_y(x), _sort_x(x)))
    return blocks


def _blocks_from_overall_ocr(overall: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for i, line in enumerate(_text_lines_from_overall_ocr(overall)):
        blocks.append(
            {
                "type": "text",
                "text": line["text"],
                "score": line["score"],
                "bbox": line["bbox"],
                "rect": line["rect"],
                "polygon": line["polygon"],
                "order": i,
                "coordinate_space": COORDINATE_SPACE,
            }
        )
    return blocks


def _text_lines_from_overall_ocr(overall: dict[str, Any]) -> list[dict[str, Any]]:
    """Return raw OCR lines without mixing them into layout blocks."""

    return [{**row, "line_index": i, "source": "overall_ocr_res"}
            for i, row in enumerate(recognized_lines(overall))]


def _structure_details(data: Mapping[str, Any]) -> dict[str, Any]:
    """Keep bounded JSON-native table/formula/seal/region payloads."""

    details: dict[str, Any] = {}
    for key, value in data.items():
        key_text = str(key)
        if key_text in _STRUCTURE_DETAIL_KEYS:
            details[key_text] = _json_native(value)
    return details


def _json_native(value: Any) -> Any:
    """Convert Paddle/numpy containers into JSON-native values."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_native(item) for item in value]
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        return _json_native(tolist())
    item = getattr(value, "item", None)
    if callable(item):
        return _json_native(item())
    return str(value)


def _optional_score(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _geometry_fields(bbox: Any, polygon: Any) -> tuple[Any, Any, list[int] | None]:
    norm_bbox = _norm_box(bbox)
    norm_polygon = _norm_poly(polygon)
    return norm_bbox, norm_polygon, _rect_from_geometry(norm_bbox or norm_polygon)


def _rect_from_geometry(geometry: Any) -> list[int] | None:
    if not isinstance(geometry, list) or not geometry:
        return None
    if len(geometry) == 4 and all(not isinstance(value, list) for value in geometry):
        return [int(round(float(value))) for value in geometry]
    points = [point for point in geometry if isinstance(point, list) and len(point) >= 2]
    if not points:
        return None
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [int(round(min(xs))), int(round(min(ys))), int(round(max(xs))), int(round(max(ys)))]


def _is_non_text_label(label: str, btype: str) -> bool:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", label.casefold())
    tokens = set(normalized.split())
    return btype in _NON_TEXT_LABELS or bool(tokens & _NON_TEXT_LABELS)


def _block_type(label: str) -> str:
    normalized = label.lower().strip()
    if "title" in normalized:
        return "title"
    if "table" in normalized:
        return "table"
    if "formula" in normalized:
        return "formula"
    if "seal" in normalized:
        return "seal"
    if "figure" in normalized or "image" in normalized:
        return "figure"
    return normalized or "text"


def _strip_html(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", "", value)
    return html.unescape(no_tags)


def _sort_order(block: dict[str, Any]) -> tuple[bool, int]:
    order = block.get("order")
    return order is None, int(order or 0)


def _sort_y(block: dict[str, Any]) -> int:
    bbox = block.get("bbox")
    if isinstance(bbox, list) and bbox:
        first = bbox[0]
        if isinstance(first, list) and len(first) >= 2:
            return int(first[1])
        if len(bbox) >= 2:
            return int(bbox[1])
    return 0


def _sort_x(block: dict[str, Any]) -> int:
    bbox = block.get("bbox")
    if isinstance(bbox, list) and bbox:
        first = bbox[0]
        if isinstance(first, list) and first:
            return int(first[0])
        if bbox:
            return int(bbox[0])
    return 0


def _norm_box(box):
    if box is None:
        return None
    if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
        return _norm_poly(box)
    return [int(round(float(v))) for v in box]


def _norm_poly(poly):
    if poly is None:
        return None
    return [[float(p[0]), float(p[1])] for p in poly]
