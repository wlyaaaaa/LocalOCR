from __future__ import annotations

import html
import re
from typing import Any

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
        engine = self._ensure()
        res = engine.predict(image_path)
        item = res[0]
        data = item.json["res"] if hasattr(item, "json") else dict(item)
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, dict) else None

        blocks = _blocks_from_parsing(data.get("parsing_res_list") or [])
        if not blocks:
            blocks = _blocks_from_overall_ocr(data.get("overall_ocr_res") or {})

        page: dict[str, Any] = {
            "page_index": int(data.get("page_index") or 0),
            "blocks": blocks,
            "structure_keys": sorted(str(k) for k in data.keys()),
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


def _blocks_from_parsing(parsing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for i, raw in enumerate(parsing):
        label = str(raw.get("block_label") or "text")
        btype = _block_type(label)
        content = str(raw.get("block_content") or "")
        text = content if btype in {"table", "formula"} else _strip_html(content)
        blocks.append(
            {
                "type": btype,
                "label": label,
                "text": text.strip(),
                "score": None,
                "bbox": _norm_box(raw.get("block_bbox")),
                "polygon": _norm_poly(raw.get("block_polygon_points")),
                "order": raw.get("block_order"),
                "block_id": raw.get("block_id"),
                "group_id": raw.get("group_id"),
            }
        )
    blocks.sort(key=lambda x: (_sort_order(x), _sort_y(x), _sort_x(x)))
    return blocks


def _blocks_from_overall_ocr(overall: dict[str, Any]) -> list[dict[str, Any]]:
    polys = overall.get("dt_polys") or overall.get("rec_polys") or []
    texts = overall.get("rec_texts") or []
    scores = overall.get("rec_scores") or []
    boxes = overall.get("rec_boxes") or []
    blocks: list[dict[str, Any]] = []
    n = max(len(texts), len(polys), len(boxes))
    for i in range(n):
        poly = polys[i] if i < len(polys) else None
        box = boxes[i] if i < len(boxes) else None
        score = float(scores[i]) if i < len(scores) else 0.0
        blocks.append(
            {
                "type": "text",
                "text": str(texts[i] if i < len(texts) else ""),
                "score": round(score, 6),
                "bbox": _norm_poly(poly) if poly else _norm_box(box),
                "order": i,
            }
        )
    return blocks


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
    return [int(round(float(v))) for v in box]


def _norm_poly(poly):
    if poly is None:
        return None
    return [[float(p[0]), float(p[1])] for p in poly]
