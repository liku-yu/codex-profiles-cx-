"""Generate a Codex model catalog so custom models appear in ``/model``.

Codex builds its picker from a `ModelsResponse` (``{"models": [ModelInfo, ...]}``).
When the top-level config key ``model_catalog_json`` points at such a file, Codex
uses it as an *authoritative* catalog (``StaticModelsManager``) and shows exactly
those models instead of the bundled GPT catalog.

Writing a valid ``ModelInfo`` by hand is fragile (many required fields change
between versions), so we clone a real template model that Codex itself produced
and only override the per-model bits.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cx import state
from cx.codex import find_codex

_DATA = Path(__file__).parent / "data"
_FALLBACK_TEMPLATE = _DATA / "catalog_template.json"

_EFFORT_DESCRIPTIONS = {
    "none": "No extra reasoning",
    "minimal": "Minimal reasoning",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth",
    "max": "Maximum reasoning depth for the hardest problems",
    "ultra": "Maximum reasoning with automatic task delegation",
    "persistent": "Persistent reasoning across the session",
}


def catalog_path(profile_name: str) -> Path:
    return state.config_dir() / "catalogs" / f"{profile_name}.json"


def _template_cache_path() -> Path:
    return state.config_dir() / "catalog_template.json"


def _template_from_codex() -> dict[str, Any] | None:
    """Read the bundled catalog from an isolated CODEX_HOME (ignores user config)."""
    codex = find_codex()
    if not codex:
        return None
    try:
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "CODEX_HOME": home}
            proc = subprocess.run(
                [codex, "debug", "models"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    models = data.get("models")
    if isinstance(models, list) and models:
        return models[0]
    return None


def template_model() -> dict[str, Any]:
    """A full ModelInfo to clone. Prefer a live/once-cached Codex template."""
    cache = _template_cache_path()
    if cache.is_file():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    template = _template_from_codex()
    if template is None:
        template = json.loads(_FALLBACK_TEMPLATE.read_text(encoding="utf-8"))
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(template), encoding="utf-8")
    except OSError:
        pass
    return template


def build_models_response(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a ModelsResponse for the given model dicts."""
    template = template_model()
    entries: list[dict[str, Any]] = []
    for model in models:
        slug = str(model.get("slug") or "").strip()
        if not slug:
            continue
        entry = copy.deepcopy(template)
        levels = [str(x) for x in (model.get("levels") or []) if str(x).strip()]
        default = model.get("default") or (levels[0] if levels else None)
        entry["slug"] = slug
        entry["display_name"] = str(model.get("display_name") or slug)
        entry["description"] = str(model.get("description") or f"{slug} (custom provider)")
        entry["supported_reasoning_levels"] = [
            {"effort": effort, "description": _EFFORT_DESCRIPTIONS.get(effort, "")}
            for effort in levels
        ]
        entry["default_reasoning_level"] = default
        # Must be True so the model survives Codex's non-ChatGPT auth filter.
        entry["supported_in_api"] = True
        entry["visibility"] = "list"  # -> show in the /model picker
        entry["context_window"] = model.get("context")
        entry["max_context_window"] = None
        entry["upgrade"] = None
        entry["availability_nux"] = None
        entries.append(entry)
    return {"models": entries}


def write_catalog(profile_name: str, models: list[dict[str, Any]]) -> Path | None:
    """Write ``~/.config/cx/catalogs/<profile>.json``; None if nothing to write."""
    response = build_models_response(models)
    if not response["models"]:
        return None
    path = catalog_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(response, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def remove_catalog(profile_name: str) -> None:
    path = catalog_path(profile_name)
    if path.exists():
        path.unlink()


def models_for_profile(profile_name: str) -> list[dict[str, Any]]:
    """All models a profile knows about (catalog > preset > its own model)."""
    from cx.presets import find_preset
    from cx.profiles import load_dict

    try:
        data = load_dict(profile_name)
    except Exception:
        return []
    catalog_json = data.get("model_catalog_json")
    if catalog_json and Path(str(catalog_json)).is_file():
        models = models_from_catalog(str(catalog_json))
        if models:
            return models
    preset = find_preset(str(data.get("model_provider") or ""))
    if preset is not None:
        return [
            {
                "slug": m.slug,
                "display_name": m.display_name,
                "levels": list(m.levels),
                "default": m.default,
            }
            for m in preset.models
        ]
    model = data.get("model")
    if isinstance(model, str) and model:
        return [{"slug": model, "display_name": model, "levels": [], "default": None}]
    return []


def ensure_catalog(profile_name: str) -> Path | None:
    """Make sure a profile that uses a custom provider has a model catalog.

    Used on apply/launch so profiles created before this feature (or edited by
    hand) still populate Codex's ``/model`` picker. Models come from an existing
    catalog, else from a matching bundled preset, plus the profile's own model.
    """
    # Imported lazily to avoid an import cycle (profiles -> codex).
    from cx.presets import find_preset
    from cx.profiles import load_dict, write_profile

    try:
        data = load_dict(profile_name)
    except Exception:
        return None

    providers = data.get("model_providers")
    pid = data.get("model_provider")
    base_url = ""
    if isinstance(providers, dict) and isinstance(pid, str):
        provider = providers.get(pid)
        if isinstance(provider, dict):
            base_url = str(provider.get("base_url") or "")
    if not base_url:
        return None  # official / built-in provider: keep Codex's own catalog

    models: list[dict[str, Any]] = []
    existing = data.get("model_catalog_json")
    if existing and Path(str(existing)).is_file():
        models = models_from_catalog(str(existing))
    if not models:
        preset = find_preset(str(pid or ""))
        if preset is not None:
            models = [
                {
                    "slug": m.slug,
                    "display_name": m.display_name,
                    "levels": list(m.levels),
                    "default": m.default,
                    "context": m.context,
                }
                for m in preset.models
            ]
    current = str(data.get("model") or "").strip()
    if current and all(m.get("slug") != current for m in models):
        models.insert(0, {"slug": current, "display_name": current, "levels": [], "default": None})

    path = write_catalog(profile_name, models)
    if path is None:
        return None
    if str(data.get("model_catalog_json") or "") != str(path):
        data["model_catalog_json"] = str(path)
        write_profile(profile_name, data)
    return path


def models_from_catalog(path: str | Path) -> list[dict[str, Any]]:
    """Read back a previously written catalog into form-model dicts."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for entry in data.get("models", []):
        if not isinstance(entry, dict) or not entry.get("slug"):
            continue
        levels = [
            preset.get("effort")
            for preset in entry.get("supported_reasoning_levels", [])
            if isinstance(preset, dict) and preset.get("effort")
        ]
        out.append(
            {
                "slug": entry["slug"],
                "display_name": entry.get("display_name") or entry["slug"],
                "levels": levels,
                "default": entry.get("default_reasoning_level"),
                "context": entry.get("context_window"),
            }
        )
    return out
