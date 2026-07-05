from __future__ import annotations

from pathlib import Path

from .ppocrv6 import PPOCRv6Engine
from .vl import VLEngine

__all__ = ["PPOCRv6Engine", "VLEngine", "get_engine"]


def get_engine(name: str, device: str = "gpu:0"):
    if name == "ocr":
        return PPOCRv6Engine(device=device)
    if name == "vl":
        return VLEngine(device=device)
    raise ValueError(f"未知引擎：{name}（应为 ocr 或 vl）")
