from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .smart_router import SmartRouteDecision, choose_smart_route, explain_explicit_model_route

VALID_ENGINE_KEYS = {"ocr", "vl", "structure"}
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
    runtime_backend: str = "paddle"
    revision: str = ""
    artifact_paths: tuple[str, ...] = ()
    preprocessing: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRegistry:
    defaults: dict[str, str]
    profiles: dict[str, ModelProfile]


def load_model_profiles(path: str | Path | None = None) -> ModelRegistry:
    profile_path = Path(path) if path is not None else DEFAULT_PROFILE_PATH
    return _parse_profiles(profile_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _parse_profiles(content: str) -> ModelRegistry:
    raw = json.loads(content)

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
            runtime_backend=str(item.get("runtime_backend", "paddle")),
            revision=str(item.get("revision", "")),
            artifact_paths=tuple(str(v) for v in item.get("artifact_paths", [])),
            preprocessing=dict(item.get("preprocessing") or {}),
        )
        if profile.runtime_backend not in {"paddle", "torch"}:
            raise ValueError(f"Unsupported runtime_backend: {profile.runtime_backend}")
        if profile.preprocessing:
            dpi = profile.preprocessing.get("pdf_dpi", 216)
            pixels = profile.preprocessing.get("max_render_pixels", 24_000_000)
            if isinstance(dpi, bool) or not isinstance(dpi, (int, float)) or not 72 <= dpi <= 600:
                raise ValueError("pdf_dpi must be between 72 and 600")
            if type(pixels) is not int or not 1 <= pixels <= 100_000_000:
                raise ValueError("max_render_pixels must be a positive bounded integer")
        if profile.engine not in VALID_ENGINE_KEYS:
            raise ValueError(f"model profile {profile.id!r} has invalid engine {profile.engine!r}")
        if profile.options.get("cuda_module_loading") not in {None, "LAZY", "EAGER"}:
            raise ValueError(f"model profile {profile.id!r} has invalid cuda_module_loading")
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


def resolve_model_reference(model_ref: str, registry: ModelRegistry | None = None) -> ModelProfile:
    registry = registry or load_model_profiles()
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
    profile, _route = select_model_profile_with_route(
        path,
        engine_choice=engine_choice,
        model_choice=model_choice,
    )
    return profile


def select_model_profile_with_route(
    path: Path,
    *,
    engine_choice: str = "auto",
    model_choice: str | None = None,
    registry: ModelRegistry | None = None,
) -> tuple[ModelProfile, SmartRouteDecision]:
    registry = registry or load_model_profiles()
    if model_choice:
        profile = resolve_model_reference(model_choice, registry)
        if engine_choice != "auto" and profile.engine != engine_choice:
            raise ValueError(
                f"model profile {profile.id!r} uses engine {profile.engine!r} "
                f"and does not match engine {engine_choice!r}"
            )
        route = explain_explicit_model_route(
            path,
            engine_choice=engine_choice,
            model_choice=model_choice,
            model_engine=profile.engine,
            model_id=profile.id,
        )
        return profile, route

    route = choose_smart_route(path, engine_choice=engine_choice, model_choice=model_choice)
    profile = resolve_model_reference(route.effective_engine, registry)
    return profile, route.with_model_id(profile.id)


def get_engine(model_ref: str | ModelProfile, device: str = "gpu:0"):
    profile = model_ref if isinstance(model_ref, ModelProfile) else resolve_model_reference(model_ref)
    module_name, class_name = profile.adapter.split(":", 1)
    module = importlib.import_module(module_name)
    engine_cls = getattr(module, class_name)
    options = dict(profile.options)
    # Worker-owned CUDA initialization is not a PaddleOCR constructor argument.
    options.pop("cuda_module_loading", None)
    return engine_cls(
        device=device,
        profile_id=profile.id,
        model_name=profile.display_name,
        engine_name=profile.result_engine_name,
        pipeline_version=profile.pipeline_version,
        options=options,
    )

# Retain the explicit test/administration cache reset entrypoint.
load_model_profiles.cache_clear = _parse_profiles.cache_clear
