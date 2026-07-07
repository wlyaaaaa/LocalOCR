from __future__ import annotations

from typing import Any

__all__ = ["PPOCRv6Engine", "VLEngine", "get_engine"]


def __getattr__(name: str) -> Any:
    if name == "PPOCRv6Engine":
        from .ppocrv6 import PPOCRv6Engine

        return PPOCRv6Engine
    if name == "VLEngine":
        from .vl import VLEngine

        return VLEngine
    raise AttributeError(name)


def get_engine(name: str, device: str = "gpu:0"):
    from ..model_registry import get_engine as get_profile_engine

    return get_profile_engine(name, device=device)
