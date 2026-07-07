from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .router import route_engine

VALID_ENGINE_KEYS = {"ocr", "vl"}
DEFAULT_PROFILE_PATH = Path(__file__).with_name("model_profiles.json")


@dataclass(frozen=True)
class ModelProfile:
    id: str
    engine: str
    adapter: str
    display_name: str
    result_engine_name: str
    backend: str
    pipeline_version: str
    capabilities: tuple[str, ...]
    options: dict[str, Any]


@dataclass(frozen=True)
class ModelRegistry:
    defaults: dict[str, str]
    profiles: dict[str, ModelProfile]


@lru_cache(maxsize=1)
def load_model_profiles(path: str | Path | None = None) -> ModelRegistry:
    profile_path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    raw = json.loads(profile_path.read_text(encoding="utf-8"))

    defaults = dict(raw.get("defaults") or {})
    profiles: dict[str, ModelProfile] = {}
    for item in raw.get("profiles") or []:
        profile = ModelProfile(
            id=str(item["id"]),
            engine=str(item["engine"]),
            adapter=str(item["adapter"]),
            display_name=str(item["display_name"]),
            result_engine_name=str(item.get("result_engine_name") or item["display_name"]),
            backend=str(item.get("backend") or ""),
            pipeline_version=str(item.get("pipeline_version") or ""),
            capabilities=tuple(str(v) for v in item.get("capabilities", [])),
            options=dict(item.get("options") or {}),
        )
        if profile.engine not in VALID_ENGINE_KEYS:
            raise ValueError(f"model profile {profile.id!r} has invalid engine {profile.engine!r}")
        if profile.id in profiles:
            raise ValueError(f"duplicate model profile id: {profile.id}")
        profiles[profile.id] = profile

    for engine_key in VALID_ENGINE_KEYS:
        if engine_key not in defaults:
            raise ValueError(f"missing default model profile for engine {engine_key!r}")
        if defaults[engine_key] not in profiles:
            raise ValueError(
                f"default model profile {defaults[engine_key]!r} for engine {engine_key!r} is not declared"
            )
        if profiles[defaults[engine_key]].engine != engine_key:
            raise ValueError(
                f"default model profile {defaults[engine_key]!r} does not match engine {engine_key!r}"
            )

    return ModelRegistry(defaults=defaults, profiles=profiles)


def list_model_ids() -> list[str]:
    return sorted(load_model_profiles().profiles)


def resolve_model_reference(model_ref: str) -> ModelProfile:
    registry = load_model_profiles()
    normalized = model_ref.strip()
    profile_id = registry.defaults.get(normalized, normalized)
    try:
        return registry.profiles[profile_id]
    except KeyError as exc:
        valid = ", ".join(list_model_ids() + sorted(registry.defaults))
        raise ValueError(f"unknown model profile {model_ref!r}; valid values: {valid}") from exc


def select_model_profile(
    path: Path,
    *,
    engine_choice: str = "auto",
    model_choice: str | None = None,
) -> ModelProfile:
    if model_choice:
        profile = resolve_model_reference(model_choice)
        if engine_choice != "auto" and profile.engine != engine_choice:
            raise ValueError(
                f"model profile {profile.id!r} uses engine {profile.engine!r} "
                f"and does not match engine {engine_choice!r}"
            )
        return profile

    engine_key = route_engine(path, engine_choice)
    return resolve_model_reference(engine_key)


def get_engine(model_ref: str, device: str = "gpu:0"):
    profile = resolve_model_reference(model_ref)
    module_name, class_name = profile.adapter.split(":", 1)
    module = importlib.import_module(module_name)
    engine_cls = getattr(module, class_name)
    return engine_cls(
        device=device,
        profile_id=profile.id,
        model_name=profile.display_name,
        engine_name=profile.result_engine_name,
        pipeline_version=profile.pipeline_version,
        options=dict(profile.options),
    )
