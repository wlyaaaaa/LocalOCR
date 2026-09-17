from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Any


POLICY_VERSION = "ocr-page-quality-v2"
LOW_SCORE_THRESHOLD = 0.80
VERY_LOW_SCORE_THRESHOLD = 0.50
MEAN_SCORE_TRIGGER = 0.78
LOW_SCORE_RATIO_TRIGGER = 0.25
VERY_LOW_SCORE_RATIO_TRIGGER = 0.10


@dataclass(frozen=True)
class OCRDifficultyAssessment:
    should_escalate: bool
    reasons: tuple[str, ...]
    metrics: dict[str, int | float | str | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "should_escalate": self.should_escalate,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


def assess_ocr_difficulty(result: dict[str, Any]) -> OCRDifficultyAssessment:
    """Assess whether an OCR result deserves a second pass with the local VL model."""
    text_block_count = 0
    scores: list[float] = []
    for page in result.get("pages") or []:
        for block in page.get("blocks") or []:
            text = str(block.get("text") or "").strip()
            if not text:
                continue
            text_block_count += 1
            raw_score = block.get("score")
            if isinstance(raw_score, Real) and not isinstance(raw_score, bool):
                score = float(raw_score)
                if 0.0 <= score <= 1.0:
                    scores.append(score)

    reasons: list[str] = []
    mean_score: float | None = None
    low_score_ratio: float | None = None
    very_low_score_ratio: float | None = None

    if text_block_count == 0:
        reasons.append("empty_text")
    elif scores:
        mean_score = sum(scores) / len(scores)
        low_score_ratio = sum(score < LOW_SCORE_THRESHOLD for score in scores) / len(scores)
        very_low_score_ratio = sum(score < VERY_LOW_SCORE_THRESHOLD for score in scores) / len(scores)
        if mean_score < MEAN_SCORE_TRIGGER:
            reasons.append("mean_score")
        if low_score_ratio >= LOW_SCORE_RATIO_TRIGGER:
            reasons.append("low_score_ratio")
        if very_low_score_ratio >= VERY_LOW_SCORE_RATIO_TRIGGER:
            reasons.append("very_low_score_ratio")

    metrics: dict[str, int | float | str | None] = {
        "text_block_count": text_block_count,
        "scored_block_count": len(scores),
        "mean_score": _round_optional(mean_score),
        "low_score_threshold": LOW_SCORE_THRESHOLD,
        "low_score_ratio": _round_optional(low_score_ratio),
        "very_low_score_threshold": VERY_LOW_SCORE_THRESHOLD,
        "very_low_score_ratio": _round_optional(very_low_score_ratio),
    }
    return OCRDifficultyAssessment(
        should_escalate=bool(reasons),
        reasons=tuple(reasons),
        metrics=metrics,
    )


def _round_optional(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def assess_ocr_pages(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Do not let many easy pages hide a difficult page; uniform is only a hint."""
    assessments = []
    for index, page in enumerate(result.get("pages") or []):
        assessment = assess_ocr_difficulty({"pages": [page]})
        reasons = list(assessment.reasons)
        if page.get("routing_uniform_hint") and reasons == ["empty_text"]:
            reasons = []
        reasons.extend(page.get("structure_signals") or [])
        if assessment.metrics["text_block_count"] and not assessment.metrics["scored_block_count"]:
            reasons.append("recognition_confidence_unavailable")
        assessments.append({**assessment.to_dict(), "page_index": page.get("page_index", index),
                            "reasons": reasons, "should_escalate": bool(reasons)})
    return assessments


def page_structure_signals(page: dict[str, Any], image_path) -> list[str]:
    """Cheap content evidence for a second pass, not a layout/correctness oracle."""
    signals = []
    text = "\n".join(str(b.get("text") or "") for b in page.get("blocks") or [])
    if any(token in text for token in ("∑", "Σ", "∫", "\\frac", "softmax(", "sqrt(")):
        signals.append("formula_content")
    try:
        import cv2
        import numpy as np
        image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return signals
        # Only the routing preview is reduced; the OCR input retains its resolution.
        factor = min(1.0, 1600 / max(image.shape))
        if factor < 1:
            image = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
        mask = cv2.threshold(image, 185, 255, cv2.THRESH_BINARY_INV)[1]
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((1, max(30, image.shape[1] // 10)), np.uint8))
        vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((max(30, image.shape[0] // 10), 1), np.uint8))
        crosses = cv2.bitwise_and(horizontal, vertical)
        count = cv2.connectedComponents(crosses)[0] - 1
        if count >= 6:
            signals.append("ruled_table_content")
    except Exception:
        pass  # Optional preview is never a reason to lose completed OCR output.
    return signals
