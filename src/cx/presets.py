"""Bundled provider presets (official + popular third-party).

Data lives in ``data/providers.json`` and was distilled from the community
presets used by cc-switch, but adapted to this tool's profile-v2 model: we set
``env_key`` (never write keys to auth.json) and only use Codex's native
``responses`` wire API.

``native_responses=False`` marks providers that only speak Chat Completions —
Codex 0.16x can no longer talk to those directly (``wire_api = "chat"`` was
removed), so they need a local translation proxy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_PATH = Path(__file__).parent / "data" / "providers.json"


@dataclass
class PresetModel:
    slug: str
    display_name: str = ""
    levels: list[str] = field(default_factory=list)
    default: str | None = None
    context: int | None = None


@dataclass
class ProviderPreset:
    id: str
    name: str
    category: str = ""
    official: bool = False
    website: str = ""
    key_url: str = ""
    base_url: str = ""
    env_key: str = ""
    wire_api: str = "responses"
    api_format: str = "openai_responses"
    native_responses: bool = True
    default_model: str = ""
    models: list[PresetModel] = field(default_factory=list)


def _to_model(raw: dict[str, Any]) -> PresetModel:
    return PresetModel(
        slug=str(raw.get("slug", "")),
        display_name=str(raw.get("display_name") or raw.get("slug", "")),
        levels=list(raw.get("levels") or []),
        default=raw.get("default"),
        context=raw.get("context"),
    )


def _to_preset(raw: dict[str, Any]) -> ProviderPreset:
    return ProviderPreset(
        id=str(raw["id"]),
        name=str(raw["name"]),
        category=str(raw.get("category", "")),
        official=bool(raw.get("official", False)),
        website=str(raw.get("website", "")),
        key_url=str(raw.get("key_url", "")),
        base_url=str(raw.get("base_url", "")),
        env_key=str(raw.get("env_key", "")),
        wire_api=str(raw.get("wire_api", "responses")),
        api_format=str(raw.get("api_format", "openai_responses")),
        native_responses=bool(raw.get("native_responses", True)),
        default_model=str(raw.get("default_model", "")),
        models=[_to_model(m) for m in raw.get("models", [])],
    )


@lru_cache(maxsize=1)
def load_presets() -> list[ProviderPreset]:
    with _DATA_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return [_to_preset(entry) for entry in raw]


def official_preset() -> ProviderPreset:
    for preset in load_presets():
        if preset.official:
            return preset
    raise RuntimeError("no official preset in bundled data")


def community_presets() -> list[ProviderPreset]:
    return [p for p in load_presets() if not p.official]


def find_preset(preset_id: str) -> ProviderPreset | None:
    for preset in load_presets():
        if preset.id == preset_id:
            return preset
    return None
