from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from paddleocr import PaddleOCR

MODEL_NAME = "PP-OCRv6_medium (det + rec)"
PIPELINE_VERSION = "PP-OCRv6"


class PPOCRv6Engine:
    """PP-OCRv6_medium 检测+识别引擎。方向检测/文档矫正/文本行旋转全开（需求 5）。"""

    def __init__(self, device: str = "gpu:0"):
        self.device = device
        self._ocr: PaddleOCR | None = None

    def _ensure(self):
        if self._ocr is None:
            self._ocr = PaddleOCR(
                ocr_version=PIPELINE_VERSION,
                lang="ch",
                use_doc_orientation_classify=True,
                use_doc_unwarping=True,
                use_textline_orientation=True,
                device=self.device,
            )
        return self._ocr

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def predict_image(self, image_path: str) -> dict[str, Any]:
        ocr = self._ensure()
        res = ocr.predict(image_path)
        item = res[0]
        data = item.json["res"] if hasattr(item, "json") else dict(item)
        polys = data.get("dt_polys") or data.get("rec_polys") or []
        texts = data.get("rec_texts") or []
        scores = data.get("rec_scores") or []
        boxes = data.get("rec_boxes") or []
        angle = None
        dpr = data.get("doc_preprocessor_res")
        if isinstance(dpr, dict):
            angle = dpr.get("angle")
        blocks = []
        n = max(len(texts), len(polys))
        for i in range(n):
            text = texts[i] if i < len(texts) else ""
            score = float(scores[i]) if i < len(scores) else 0.0
            poly = polys[i] if i < len(polys) else None
            box = boxes[i] if i < len(boxes) else None
            blocks.append({
                "type": "text",
                "text": str(text),
                "score": round(score, 6),
                "bbox": _norm_poly(poly) if poly else (list(box) if box else None),
                "order": i,
            })
        return {
            "engine": "PP-OCRv6_medium",
            "model": MODEL_NAME,
            "device": self.device,
            "page_angle": angle,
            "pages": [{"page_index": 0, "blocks": blocks}],
        }


def _norm_poly(poly):
    if poly is None:
        return None
    return [[int(round(float(p[0]))), int(round(float(p[1])))] for p in poly]
