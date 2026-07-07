from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .router import is_image, is_pdf

VALID_REQUESTED_ENGINES = {"auto", "ocr", "vl", "structure"}
COMPLEX_PDF_KEYWORDS = (
    "table",
    "formula",
    "layout",
    "multi",
    "column",
    "lecture",
    "paper",
    "论文",
    "公式",
    "表格",
    "多栏",
    "课件",
)


@dataclass(frozen=True)
class SmartRouteDecision:
    requested_engine: str
    requested_model: str | None
    effective_engine: str
    route_reason: str
    confidence: float
    signals: tuple[str, ...]
    model_id: str | None = None

    def with_model_id(self, model_id: str) -> "SmartRouteDecision":
        return replace(self, model_id=model_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_engine": self.requested_engine,
            "requested_model": self.requested_model,
            "effective_engine": self.effective_engine,
            "reason": self.route_reason,
            "route_reason": self.route_reason,
            "confidence": self.confidence,
            "signals": list(self.signals),
            "model_id": self.model_id,
        }


def choose_smart_route(
    path: Path,
    *,
    engine_choice: str = "auto",
    model_choice: str | None = None,
) -> SmartRouteDecision:
    if engine_choice not in VALID_REQUESTED_ENGINES:
        raise ValueError(f"engine 必须是 auto/ocr/vl/structure，得到 {engine_choice!r}")

    if model_choice:
        return SmartRouteDecision(
            requested_engine=engine_choice,
            requested_model=model_choice,
            effective_engine=engine_choice,
            route_reason="explicit_model",
            confidence=1.0,
            signals=("explicit_model",),
        )

    if engine_choice != "auto":
        return SmartRouteDecision(
            requested_engine=engine_choice,
            requested_model=None,
            effective_engine=engine_choice,
            route_reason=f"explicit_{engine_choice}",
            confidence=1.0,
            signals=("explicit_engine",),
        )

    if is_image(path):
        return SmartRouteDecision(
            requested_engine=engine_choice,
            requested_model=None,
            effective_engine="ocr",
            route_reason="image_prefers_ocr",
            confidence=0.9,
            signals=_base_signals(path, "image"),
        )

    if is_pdf(path):
        signals = list(_base_signals(path, "pdf"))
        keyword_signals = _complex_keyword_signals(path)
        signals.extend(keyword_signals)
        if keyword_signals:
            return SmartRouteDecision(
                requested_engine=engine_choice,
                requested_model=None,
                effective_engine="vl",
                route_reason="pdf_complex_layout_prefers_vl",
                confidence=0.82,
                signals=tuple(signals),
            )
        signals.append("plain_pdf_default")
        return SmartRouteDecision(
            requested_engine=engine_choice,
            requested_model=None,
            effective_engine="ocr",
            route_reason="pdf_plain_text_prefers_ocr",
            confidence=0.72,
            signals=tuple(signals),
        )

    return SmartRouteDecision(
        requested_engine=engine_choice,
        requested_model=None,
        effective_engine="ocr",
        route_reason="unknown_type_prefers_ocr",
        confidence=0.5,
        signals=_base_signals(path, "unknown_type"),
    )


def explain_explicit_model_route(
    path: Path,
    *,
    engine_choice: str,
    model_choice: str,
    model_engine: str,
    model_id: str,
) -> SmartRouteDecision:
    if engine_choice not in VALID_REQUESTED_ENGINES:
        raise ValueError(f"engine 必须是 auto/ocr/vl/structure，得到 {engine_choice!r}")
    return SmartRouteDecision(
        requested_engine=engine_choice,
        requested_model=model_choice,
        effective_engine=model_engine,
        route_reason="explicit_model",
        confidence=1.0,
        signals=_base_signals(path, "explicit_model"),
        model_id=model_id,
    )


def _base_signals(path: Path, kind: str) -> tuple[str, ...]:
    signals = [kind, f"ext:{path.suffix.lower() or '<none>'}"]
    try:
        size = path.stat().st_size
    except OSError:
        return tuple(signals)
    if size <= 512 * 1024:
        signals.append("small_file")
    elif size >= 8 * 1024 * 1024:
        signals.append("large_file")
    return tuple(signals)


def _complex_keyword_signals(path: Path) -> list[str]:
    name = path.name.casefold()
    return [f"complex_keyword:{keyword}" for keyword in COMPLEX_PDF_KEYWORDS if keyword in name]
