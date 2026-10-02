"""Opt-in: merge a profile-v2 file into the base ``config.toml``.

This exists so that *plain* ``codex`` (and the VS Code extension / app) can pick
up a profile without passing ``--profile``. It is the only code in this tool
that writes ``config.toml`` and it is deliberately conservative:

  * the current ``config.toml`` is snapshotted first, outside $CODEX_HOME;
  * the merge uses tomlkit and preserves comments / ordering of untouched keys;
  * the final document is validated against Codex's schema before writing;
  * ``unapply`` restores the exact pre-apply snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import tomlkit

from cx import state
from cx.codex import codex_home
from cx.profiles import load_dict
from cx.schema import provider_reference_issues, validate_document

CONFIG_NAME = "config.toml"

# Top-level keys this tool writes when applying a profile.
_MANAGED_KEYS = ("model", "model_provider", "model_reasoning_effort", "model_catalog_json")


class ApplyError(Exception):
    pass


def config_path() -> Path:
    return codex_home() / CONFIG_NAME


def apply_state_path() -> Path:
    return state.config_dir() / "apply.json"


def snapshots_dir() -> Path:
    return state.config_dir() / "config-backups"


def baseline_dir() -> Path:
    return state.config_dir() / "baseline"


def record_baseline() -> None:
    """Capture config.toml once, before cx's first write (for a true reset)."""
    directory = baseline_dir()
    if directory.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    src = config_path()
    if src.is_file():
        shutil.copy2(src, directory / CONFIG_NAME)
        _chmod_600(directory / CONFIG_NAME)
    else:
        (directory / "absent").write_text("", encoding="utf-8")


def has_baseline() -> bool:
    return baseline_dir().exists()


def applied_status() -> dict[str, Any] | None:
    path = apply_state_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_label(value: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "_-") else "-" for c in value.strip())
    return cleaned.strip("-") or "default"


def current_config_label() -> str:
    """A short name describing the current config (for snapshot filenames).

    Preference: the currently applied profile, else the config's model_provider,
    else "default" (and "empty" when there is no config.toml).
    """
    info = applied_status()
    if info and info.get("profile"):
        return _safe_label(str(info["profile"]))
    src = config_path()
    if not src.is_file():
        return "empty"
    try:
        doc = tomlkit.parse(src.read_text(encoding="utf-8"))
    except Exception:
        return "default"
    provider = doc.get("model_provider")
    if provider:
        return _safe_label(str(provider))
    return "default"


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def find_matching_snapshot(content_digest: str) -> Path | None:
    """Return an existing snapshot whose content matches, if any."""
    for snapshot in list_snapshots():
        if _file_digest(snapshot) == content_digest:
            return snapshot
    return None


def current_config_snapshot() -> Path | None:
    """Existing snapshot identical to the current config.toml, if any."""
    digest = _file_digest(config_path())
    if digest is None:
        return None
    return find_matching_snapshot(digest)


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _snapshot_current() -> Path | None:
    src = config_path()
    if not src.is_file():
        return None
    # Don't create a duplicate: if this exact content is already saved, reuse it.
    digest = _file_digest(src)
    if digest is not None:
        existing = find_matching_snapshot(digest)
        if existing is not None:
            return existing
    directory = snapshots_dir()
    directory.mkdir(parents=True, exist_ok=True)
    label = current_config_label()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = directory / f"{label}.{stamp}.toml"
    counter = 1
    while dest.exists():
        dest = directory / f"{label}.{stamp}.{counter}.toml"
        counter += 1
    shutil.copy2(src, dest)
    _chmod_600(dest)
    return dest


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.cx-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    _chmod_600(path)


def _merge_into(container: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if isinstance(value, dict):
            existing = container.get(key)
            if existing is None or not hasattr(existing, "get"):
                container[key] = tomlkit.table()
                existing = container[key]
            _merge_into(existing, value)
        else:
            container[key] = value


def _normalize_provider_auth(doc: Any, profile: dict[str, Any]) -> None:
    """Keep only one auth method per provider after merging.

    Codex prefers ``env_key`` over ``experimental_bearer_token`` and fails when
    the variable is missing, so when the profile supplies a bearer token we drop
    a stale ``env_key`` (and vice versa).
    """
    profile_providers = profile.get("model_providers")
    merged = doc.get("model_providers") if hasattr(doc, "get") else None
    if not isinstance(profile_providers, dict) or merged is None:
        return
    for pid, prof_provider in profile_providers.items():
        if not isinstance(prof_provider, dict):
            continue
        entry = merged.get(pid)
        if entry is None or not hasattr(entry, "pop"):
            continue
        if prof_provider.get("experimental_bearer_token"):
            entry.pop("env_key", None)
        elif prof_provider.get("env_key"):
            entry.pop("experimental_bearer_token", None)


def _drop_unused_providers(
    doc: Any, keep: str, candidates: list[str] | None
) -> None:
    """Remove provider tables cx added earlier that are no longer active."""
    if not candidates:
        return
    providers = doc.get("model_providers") if hasattr(doc, "get") else None
    if providers is None or not hasattr(providers, "remove"):
        return
    for pid in candidates:
        if pid and pid != keep and pid in providers:
            try:
                providers.remove(pid)
            except (KeyError, ValueError):
                pass
    try:
        if len(providers) == 0:
            doc.remove("model_providers")
    except (KeyError, ValueError):
        pass


def preview_merge(
    name: str, drop_providers: list[str] | None = None
) -> tuple[str, list[str]]:
    """Return (resulting toml text, problems) without writing anything.

    ``drop_providers`` are provider ids a previous apply added that are no
    longer the active provider; they are dropped so config.toml does not
    accumulate stale provider tables.
    """
    profile = load_dict(name)
    path = config_path()
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    _merge_into(doc, profile)
    _normalize_provider_auth(doc, profile)
    _drop_unused_providers(doc, str(profile.get("model_provider") or ""), drop_providers)
    text = tomlkit.dumps(doc)
    import tomllib

    final = tomllib.loads(text)
    problems = validate_document(final)
    problems.extend(provider_reference_issues(final, final))
    return text, problems


def apply_profile(name: str, *, force: bool = False, save_snapshot: bool = False) -> Path:
    """Merge profile ``name`` into config.toml (validate + optional snapshot)."""
    previous = applied_status() or {}
    prev_providers = previous.get("providers")
    drop = [str(p) for p in prev_providers] if isinstance(prev_providers, list) else []
    text, problems = preview_merge(name, drop_providers=drop)
    if problems and not force:
        raise ApplyError(
            "拒绝应用；合并后的配置存在问题：\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    record_baseline()
    had_config = config_path().is_file()
    snapshot = _snapshot_current() if save_snapshot else None
    _atomic_write(config_path(), text)

    try:
        providers = list((load_dict(name).get("model_providers") or {}).keys())
    except Exception:
        providers = []

    apply_state_path().parent.mkdir(parents=True, exist_ok=True)
    apply_state_path().write_text(
        json.dumps(
            {
                "profile": name,
                "snapshot": str(snapshot) if snapshot else None,
                "had_config": had_config,
                "providers": providers,
                "applied_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return config_path()


def unapply(*, save_snapshot: bool = False) -> Path | None:
    """Restore config.toml to its pre-apply state.

    Uses the snapshot saved at apply time if there is one; otherwise, if
    config.toml did not exist before, removes it; otherwise falls back to
    stripping cx's managed keys/providers.
    """
    info = applied_status()
    if not info:
        raise ApplyError("没有记录已应用的 profile")

    if save_snapshot:
        _snapshot_current()

    path = config_path()
    snapshot = info.get("snapshot")
    if snapshot and Path(snapshot).is_file():
        shutil.copy2(snapshot, path)
        apply_state_path().unlink(missing_ok=True)
        return path if path.exists() else None
    if info.get("had_config") is False:
        if path.exists():
            path.unlink()
        apply_state_path().unlink(missing_ok=True)
        return None
    # no snapshot available: best-effort strip (keeps apply.json for provider ids)
    return strip_cx_changes(save_snapshot=False)


def list_snapshots() -> list[Path]:
    directory = snapshots_dir()
    if not directory.is_dir():
        return []
    snaps = [p for p in directory.glob("*.toml") if p.is_file()]
    return sorted(snaps, key=lambda p: p.stat().st_mtime, reverse=True)


def delete_snapshot(snapshot: Path) -> None:
    """Permanently delete one config.toml snapshot."""
    if snapshot.is_file():
        snapshot.unlink()


def delete_baseline() -> None:
    """Delete the recorded pre-cx config (so `R` falls back to stripping)."""
    directory = baseline_dir()
    if directory.exists():
        shutil.rmtree(directory)


def restore_snapshot(snapshot: Path, *, save_snapshot: bool = False) -> Path:
    if not snapshot.is_file():
        raise ApplyError(f"找不到快照：{snapshot}")
    if save_snapshot:
        _snapshot_current()
    shutil.copy2(snapshot, config_path())
    apply_state_path().unlink(missing_ok=True)
    return config_path()


def _managed_provider_ids() -> list[str]:
    """Provider ids that cx wrote into config.toml (best effort)."""
    info = applied_status() or {}
    ids: list[str] = []
    recorded = info.get("providers")
    if isinstance(recorded, list):
        ids.extend(str(p) for p in recorded)
    profile = info.get("profile")
    if profile:
        try:
            providers = load_dict(str(profile)).get("model_providers")
        except Exception:
            providers = None
        if isinstance(providers, dict):
            ids.extend(str(k) for k in providers)
    return list(dict.fromkeys(ids))


def strip_cx_changes(*, save_snapshot: bool = False) -> Path | None:
    """Remove cx's managed keys and providers from config.toml, keep the rest."""
    path = config_path()
    if not path.is_file():
        return None
    if save_snapshot:
        _snapshot_current()

    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    for key in _MANAGED_KEYS:
        if key in doc:
            doc.remove(key)

    provider_ids = _managed_provider_ids()
    providers = doc.get("model_providers")
    if providers is not None:
        for pid in provider_ids:
            try:
                if pid in providers:
                    providers.remove(pid)
            except (KeyError, ValueError):
                pass
        try:
            if len(providers) == 0:
                doc.remove("model_providers")
        except (KeyError, ValueError):
            pass

    _atomic_write(path, tomlkit.dumps(doc))
    apply_state_path().unlink(missing_ok=True)
    return path


def restore_original(*, save_snapshot: bool = False) -> Path | None:
    """Restore Codex's pre-cx config.

    If a baseline was captured (config.toml as it was before cx first wrote to
    it), restore it exactly; otherwise fall back to stripping the keys/providers
    cx manages, preserving everything else.
    """
    directory = baseline_dir()
    if directory.exists():
        if save_snapshot:
            _snapshot_current()
        path = config_path()
        if (directory / CONFIG_NAME).is_file():
            shutil.copy2(directory / CONFIG_NAME, path)
        elif path.exists():
            path.unlink()
        apply_state_path().unlink(missing_ok=True)
        return path if path.exists() else None
    return strip_cx_changes(save_snapshot=save_snapshot)
