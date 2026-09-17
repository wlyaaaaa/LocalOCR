"""Small upstream-result adapter contract shared by the three Paddle pipelines."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def native(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [native(v) for v in value]
    convert = getattr(value, "tolist", None)
    return native(convert()) if callable(convert) else value


def result_data(item: Any) -> dict:
    value = getattr(item, "json", item)
    if callable(value):
        value = value()
    value = native(value)
    if not isinstance(value, Mapping):
        raise ValueError("OCR adapter expected an object result")
    data = value.get("res", value)
    if not isinstance(data, Mapping):
        raise ValueError("OCR adapter expected an object in res")
    return dict(data)


def optional_score(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return round(score, 6) if math.isfinite(score) and 0 <= score <= 1 else None


def recognized_lines(data: Mapping[str, Any]) -> list[dict]:
    """Bind geometry to accepted recognition rows, never unfiltered detections."""
    data = native(data)
    texts = data.get("rec_texts") or []
    scores = data.get("rec_scores") or []
    boxes = data.get("rec_boxes") or []
    polys = data.get("rec_polys")
    if polys is None:
        detected = data.get("dt_polys") or []
        # Old SDKs omit rec_polys. A full, unfiltered one-to-one set is safe.
        polys = detected if len(detected) == len(texts) else []
    for name, values in (("rec_scores", scores), ("rec_boxes", boxes), ("rec_polys", polys)):
        if values and len(values) != len(texts):
            raise ValueError(f"OCR adapter alignment error: {name}={len(values)}, rec_texts={len(texts)}")
    rows = []
    for index, text in enumerate(texts):
        poly = polys[index] if polys else None
        box = boxes[index] if boxes else None
        if poly is not None:
            poly = [[int(round(float(p[0]))), int(round(float(p[1])))] for p in poly]
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            rect = [min(xs), min(ys), max(xs), max(ys)] if poly else None
        else:
            rect = [int(round(float(v))) for v in box] if box is not None else None
        rows.append({"text": str(text), "score": optional_score(scores[index]) if scores else None,
                     "bbox": poly or rect, "rect": rect, "polygon": poly,
                     "order": index, "coordinate_space": "image_pixels"})
    return rows


def combine_predictions(predictions: Any, convert, *, options: Mapping[str, Any]) -> dict:
    """Consume generators and every SDK result, retaining each page's own frame."""
    combined: dict = {}
    pages: list[dict] = []
    for index, item in enumerate(predictions):
        data = result_data(item)
        result = convert(data)
        if not combined:
            combined = {k: v for k, v in result.items() if k != "pages"}
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, Mapping) else None
        settings = dpr.get("model_settings") or {} if isinstance(dpr, Mapping) else {}
        unwarped = bool(dpr) and bool(settings.get("use_doc_unwarping", options.get("use_doc_unwarping", False)))
        rotated = angle not in (None, -1, 0)
        reference = {"frame": "document_preprocessed" if unwarped or rotated else "source_image",
                     "rotation_degrees": angle, "unwarped": unwarped,
                     "original_mapping_status": "unavailable_non_linear_transform" if unwarped else
                         ("rotation_not_mapped" if rotated else "identity")}
        for page in result.get("pages") or []:
            page["page_index"] = len(pages)
            page["page_angle"] = angle
            page["coordinate_reference"] = reference
            for key in ("blocks", "text_lines", "excluded_regions"):
                for region in page.get(key) or []:
                    region["coordinate_reference"] = reference
            pages.append(page)
    combined["pages"] = pages
    combined["returned_page_count"] = len(pages)
    return combined
