"""Detect which models a provider/key can access, and their reasoning levels.

Two sources are tried:

1. A Codex-native model catalog (``model_catalog_url`` or an OpenAI-compatible
   ``/models`` that returns the Codex shape). Its entries carry
   ``supported_reasoning_levels`` / ``default_reasoning_level``.
2. A plain OpenAI-compatible ``GET {base_url}/models`` list (ids only); the
   reasoning levels are then guessed from the model family.

Everything is stdlib-only (urllib), so no extra dependency is needed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# Wire values accepted by Codex's ReasoningEffort (protocol/src/openai_models.rs).
KNOWN_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "persistent"]


@dataclass
class DetectedModel:
    slug: str
    display_name: str = ""
    efforts: list[str] = field(default_factory=list)
    default_effort: str | None = None
    source: str = "list"  # "catalog" | "list" | "guess"


@dataclass
class DetectResult:
    models: list[DetectedModel]
    errors: list[str] = field(default_factory=list)


def _http_get_json(url: str, api_key: str, timeout: float) -> object:
    headers = {
        "Accept": "application/json",
        "User-Agent": "codex-profiles",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body)


# Base URLs ending in these compatibility suffixes get the suffix stripped and
# re-joined before retrying (mirrors cc-switch's candidate builder).
_COMPAT_SUFFIXES = ("/api/anthropic", "/apps/anthropic", "/anthropic")
_VERSION_RE = re.compile(r"/v\d+$")


def _models_urls(base_url: str) -> list[str]:
    base = base_url.strip().rstrip("/")
    if not base:
        return []

    candidates: list[str] = []
    if _VERSION_RE.search(base):
        # base already ends in a version segment (e.g. /v1, /v4): use {base}/models
        candidates.append(f"{base}/models")
        if not base.endswith("/v1"):
            candidates.append(f"{base}/v1/models")
    else:
        candidates.append(f"{base}/v1/models")

    for suffix in _COMPAT_SUFFIXES:
        if base.endswith(suffix):
            root = base[: -len(suffix)].rstrip("/")
            if root and "://" in root:
                candidates.append(f"{root}/v1/models")
                candidates.append(f"{root}/models")
            break

    # de-duplicate, preserving order
    unique: list[str] = []
    for url in candidates:
        if url not in unique:
            unique.append(url)
    return unique


def _parse_payload(data: object, source: str) -> list[DetectedModel]:
    entries: list[object] = []
    if isinstance(data, dict):
        for key in ("models", "data"):
            value = data.get(key)
            if isinstance(value, list):
                entries = value
                break
    elif isinstance(data, list):
        entries = data

    out: list[DetectedModel] = []
    for entry in entries:
        if isinstance(entry, str):
            out.append(DetectedModel(entry, entry, guess_efforts(entry), None, "guess"))
            continue
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug") or entry.get("id") or entry.get("model")
        if not isinstance(slug, str) or not slug:
            continue
        levels = entry.get("supported_reasoning_levels")
        efforts: list[str] = []
        if isinstance(levels, list):
            for preset in levels:
                if isinstance(preset, dict) and isinstance(preset.get("effort"), str):
                    efforts.append(preset["effort"])
        if not efforts:
            efforts = guess_efforts(slug)
        default = entry.get("default_reasoning_level")
        out.append(
            DetectedModel(
                slug=slug,
                display_name=str(entry.get("display_name") or slug),
                efforts=efforts,
                default_effort=default if isinstance(default, str) else None,
                source=source if entry.get("supported_reasoning_levels") else "guess",
            )
        )
    return out


def guess_efforts(slug: str) -> list[str]:
    """Best-effort reasoning levels from a model id (used when no catalog)."""
    s = slug.lower()
    if "codex" in s and "gpt-5" in s:
        if "max" in s or "5.1" in s:
            return ["low", "medium", "high", "xhigh"]
        return ["minimal", "low", "medium", "high"]
    if "gpt-5" in s:
        return ["minimal", "low", "medium", "high"]
    if s.startswith(("o3", "o4")):
        return ["low", "medium", "high"]
    if s.startswith("r1") or "reasoner" in s or "reasoning" in s:
        return ["low", "medium", "high"]
    if "gpt-4" in s or s.startswith("gpt-3"):
        return []  # non-reasoning family
    return []


def detect_models(
    base_url: str | None,
    api_key: str,
    model_catalog_url: str | None = None,
    timeout: float = 15.0,
) -> DetectResult:
    """Return detected models merged from the catalog and the models list."""
    errors: list[str] = []
    found: dict[str, DetectedModel] = {}

    if model_catalog_url:
        try:
            data = _http_get_json(model_catalog_url, api_key, timeout)
            for model in _parse_payload(data, "catalog"):
                found[model.slug] = model
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errors.append(f"catalog {model_catalog_url}: {exc}")

    if base_url:
        for url in _models_urls(base_url):
            try:
                data = _http_get_json(url, api_key, timeout)
            except (urllib.error.URLError, ValueError, OSError) as exc:
                errors.append(f"{url}: {exc}")
                continue
            parsed = _parse_payload(data, "list")
            if not parsed:
                errors.append(f"{url}: no models in response")
                continue
            for model in parsed:
                found.setdefault(model.slug, model)
            break

    models = sorted(found.values(), key=lambda m: m.slug)
    return DetectResult(models=models, errors=errors)
