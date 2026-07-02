from __future__ import annotations

from typing import Any

from paddleocr import PaddleOCRVL

MODEL_NAME = "PaddleOCR-VL-1.6"
PIPELINE_VERSION = "v1.6"


class VLEngine:
    """PaddleOCR-VL-1.6 引擎，用于 PDF/合同/论文/表格/公式/多栏复杂文档（需求 3）。"""

    def __init__(self, device: str = "gpu:0"):
        self.device = device
        self._vl: PaddleOCRVL | None = None

    def _ensure(self):
        if self._vl is None:
            self._vl = PaddleOCRVL(
                pipeline_version=PIPELINE_VERSION,
                vl_rec_backend="native",
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                device=self.device,
            )
        return self._vl

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def predict_image(self, image_path: str) -> dict[str, Any]:
        vl = self._ensure()
        res = vl.predict(image_path)
        item = res[0]
        data = item.json["res"] if hasattr(item, "json") else dict(item)
        parsing = data.get("parsing_res_list") or []
        dpr = data.get("doc_preprocessor_res") or {}
        angle = dpr.get("angle") if isinstance(dpr, dict) else None
        blocks = []
        for b in parsing:
            label = b.get("block_label", "text")
            blocks.append({
                "type": str(label),
                "text": str(b.get("block_content", "")),
                "score": None,
                "bbox": _norm_box(b.get("block_bbox")),
                "polygon": _norm_poly(b.get("block_polygon_points")),
                "order": b.get("block_order"),
                "block_id": b.get("block_id"),
                "group_id": b.get("group_id"),
            })
        blocks.sort(key=lambda x: (x.get("order") is None, x.get("order") or 0))
        return {
            "engine": "PaddleOCR-VL-1.6",
            "model": MODEL_NAME,
            "device": self.device,
            "page_angle": angle,
            "page_width": data.get("width"),
            "page_height": data.get("height"),
            "pages": [{"page_index": 0, "blocks": blocks}],
        }


def _norm_box(box):
    if box is None:
        return None
    return [int(round(float(v))) for v in box]


def _norm_poly(poly):
    if poly is None:
        return None
    return [[float(p[0]), float(p[1])] for p in poly]
