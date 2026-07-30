from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Any


POLICY_VERSION = "ocr-confidence-v1"
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
