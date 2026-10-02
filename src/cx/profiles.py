"""Create / read / write profile-v2 files safely.

Guarantees:
  * Only files named ``<safe-name>.config.toml`` directly under $CODEX_HOME are
    ever written. ``config.toml`` and ``auth.json`` are never modified.
  * Writes are atomic (temp file + os.replace).
  * Overwrites are backed up first, outside $CODEX_HOME.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path
from typing import Any

import tomlkit

from cx import state
from cx.codex import (
    PROFILE_NAME_RE,
    codex_home,
    list_profile_names,
    profile_path,
)


class ProfileError(Exception):
    pass


def _chmod_600(path: Path) -> None:
    """Profiles may hold a literal API key; keep them owner-only."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def validate_name(name: str) -> str:
    if not name or not PROFILE_NAME_RE.match(name):
        raise ProfileError(
            f"profile 名称无效 {name!r}：只能使用字母、数字、'_' 和 '-'"
        )
    return name


def resolve(name: str) -> Path:
    validate_name(name)
    return profile_path(name)


def load_text(name: str) -> str:
    path = resolve(name)
    if not path.is_file():
        raise ProfileError(f"profile {name!r} 不存在（{path}）")
    return path.read_text(encoding="utf-8")


def load_dict(name: str) -> dict[str, Any]:
    raw = load_text(name).encode("utf-8")
    return tomllib.loads(raw.decode("utf-8"))


def write_profile(name: str, data: dict[str, Any], *, backup: bool = True) -> Path:
    """Atomically write ``<name>.config.toml`` after validation and backup."""
    validate_name(name)
    text = tomlkit.dumps(data)
    return write_text(name, text, backup=backup)


def write_text(name: str, text: str, *, backup: bool = True) -> Path:
    validate_name(name)
    path = resolve(name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and backup:
        dest = state.backup_path(name)
        shutil.copy2(path, dest)
        _chmod_600(dest)

    tmp = path.with_name(f".{path.name}.cx-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    _chmod_600(path)
    return path


def delete_profile(name: str, *, purge: bool = False) -> Path | None:
    """Delete a profile file. By default it is moved to the backup dir."""
    path = resolve(name)
    if not path.is_file():
        raise ProfileError(f"profile {name!r} 不存在")
    if purge:
        path.unlink()
        return None
    dest = state.backup_path(name)
    shutil.move(str(path), str(dest))
    return dest


def summarize(name: str) -> dict[str, str]:
    """Cheap summary from the file itself (no codex invocation)."""
    summary: dict[str, str] = {}
    try:
        data = load_dict(name)
    except (ProfileError, tomllib.TOMLDecodeError):
        return summary
    for key in ("model", "model_provider", "model_reasoning_effort"):
        value = data.get(key)
        if isinstance(value, str):
            summary[key] = value
    provider = data.get("model_provider")
    providers = data.get("model_providers")
    if isinstance(provider, str) and isinstance(providers, dict):
        entry = providers.get(provider)
        if isinstance(entry, dict) and isinstance(entry.get("base_url"), str):
            summary["base_url"] = entry["base_url"]
    return summary


def read_base_config() -> dict[str, Any] | None:
    """Read (never write) the base $CODEX_HOME/config.toml."""
    path = codex_home() / "config.toml"
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return None


def existing_names() -> list[str]:
    return list_profile_names()


def build_profile_document(
    *,
    name: str,
    model: str | None = None,
    effort: str | None = None,
    provider_id: str | None = None,
    provider_name: str | None = None,
    base_url: str | None = None,
    env_key: str | None = None,
    api_key: str | None = None,
    wire_api: str | None = "responses",
    model_catalog_url: str | None = None,
    model_catalog_json: str | None = None,
) -> dict[str, Any]:
    """Build the TOML document for a profile (shared by CLI and TUI)."""
    data: dict[str, Any] = {}
    if model:
        data["model"] = model
    if effort:
        data["model_reasoning_effort"] = effort

    pid = provider_id or (name if base_url else None)
    if pid:
        data["model_provider"] = pid
    if base_url:
        provider: dict[str, Any] = {
            "name": provider_name or pid,
            "base_url": base_url,
        }
        if api_key:
            # Codex has no plain api_key field; the literal bearer token is the
            # simplest way to give a provider a key without any env var.
            provider["experimental_bearer_token"] = api_key
        elif env_key:
            provider["env_key"] = env_key
        if model_catalog_url:
            provider["model_catalog_url"] = model_catalog_url
        if wire_api:
            provider["wire_api"] = wire_api
        data["model_providers"] = {pid: provider}
    if model_catalog_json:
        data["model_catalog_json"] = model_catalog_json
    return data
