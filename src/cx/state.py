"""Tool-local state kept *outside* $CODEX_HOME so Codex is never touched."""

from __future__ import annotations

import os
import time
from pathlib import Path


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "cx"


def active_file() -> Path:
    return config_dir() / "active"


def backups_dir() -> Path:
    return config_dir() / "backups"


def get_active() -> str | None:
    path = active_file()
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def theme_file() -> Path:
    return config_dir() / "theme"


def get_theme() -> str | None:
    path = theme_file()
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def set_theme(name: str) -> None:
    path = theme_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_active(name: str | None) -> None:
    path = active_file()
    if name is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(name + "\n", encoding="utf-8")
    os.replace(tmp, path)


def backup_path(name: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    directory = backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{name}.{stamp}.config.toml"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{name}.{stamp}.{counter}.config.toml"
        counter += 1
    return candidate
