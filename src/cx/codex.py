"""Locating and driving the real ``codex`` binary.

Everything here is read-only with respect to the user's Codex state, except
``run`` which simply execs the codex binary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# The suffix Codex uses for profile-v2 files: $CODEX_HOME/<name>.config.toml
PROFILE_SUFFIX = ".config.toml"

# ProfileV2Name accepts only ASCII alphanumerics plus '_' and '-'.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# IDs reserved by Codex itself; a user provider may not shadow them.
RESERVED_PROVIDER_IDS = frozenset(
    {
        "openai",
        "ollama",
        "lmstudio",
        "amazon-bedrock",
        "amazon-bedrock-runtime",
    }
)

# Legacy provider ids that Codex now rejects with a migration error.
REMOVED_PROVIDER_IDS = frozenset({"ollama-chat"})


def codex_home() -> Path:
    """Return $CODEX_HOME (default ~/.codex)."""
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def find_codex() -> str | None:
    """Locate the codex executable on PATH."""
    return shutil.which("codex")


def codex_version(codex_bin: str | None = None) -> str | None:
    bin_path = codex_bin or find_codex()
    if not bin_path:
        return None
    try:
        out = subprocess.run(
            [bin_path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (out.stdout or out.stderr or "").strip()
    return text or None


def profile_path(name: str) -> Path:
    """Path to the profile-v2 file for ``name`` (not checked for existence)."""
    return codex_home() / f"{name}{PROFILE_SUFFIX}"


def list_profile_names() -> list[str]:
    """Names of every ``*.config.toml`` directly under CODEX_HOME."""
    home = codex_home()
    if not home.is_dir():
        return []
    names: list[str] = []
    for entry in home.iterdir():
        if not entry.is_file() or not entry.name.endswith(PROFILE_SUFFIX):
            continue
        name = entry.name[: -len(PROFILE_SUFFIX)]
        if PROFILE_NAME_RE.match(name):
            names.append(name)
    return sorted(set(names))


@dataclass
class ProbeResult:
    ok: bool
    header: dict[str, str] = field(default_factory=dict)
    output: str = ""
    error: str | None = None


_HEADER_KEYS = {
    "model": "model",
    "provider": "provider",
    "approval": "approval",
    "sandbox": "sandbox",
    "reasoning effort": "reasoning_effort",
    "reasoning summaries": "reasoning_summaries",
    "session id": "session_id",
}


def _parse_header(lines: list[str]) -> dict[str, str]:
    header: dict[str, str] = {}
    for line in lines:
        m = re.match(r"^([A-Za-z][A-Za-z ]+):\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        if key in _HEADER_KEYS:
            header[_HEADER_KEYS[key]] = m.group(2).strip()
    return header


def probe_profile(name: str, timeout: float = 25.0, cwd: str | None = None) -> ProbeResult:
    """Ask the real codex binary to resolve ``--profile name`` and read back
    the effective model/provider.

    This runs ``codex -p <name> exec`` which prints a header line such as::

        model: gpt-5.1-codex
        provider: myrelay
        reasoning effort: high

    before any network call, then we terminate it. This is the ground truth
    that a profile actually takes effect.
    """
    codex_bin = find_codex()
    if not codex_bin:
        return ProbeResult(False, error="codex executable not found on PATH")

    env = dict(os.environ)
    # Make sure env-var based providers do not abort on a missing key: the
    # probe only needs local config resolution, never a live request.
    for var in _env_keys_for_profile(name):
        env.setdefault(var, "cx-probe-dummy")

    cmd = [
        codex_bin,
        "--profile",
        name,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "cx config probe",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=cwd,
        )
    except OSError as exc:
        return ProbeResult(False, error=f"failed to start codex: {exc}")

    lines: list[str] = []
    deadline = time.monotonic() + timeout
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)
            if "session id:" in line:
                break
            if time.monotonic() > deadline:
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.SubprocessError:
            pass

    output = "\n".join(lines)
    header = _parse_header(lines)
    if header.get("model") and header.get("provider"):
        return ProbeResult(True, header=header, output=output)
    return ProbeResult(
        False,
        header=header,
        output=output,
        error="codex did not report an effective model/provider (see output)",
    )


def _env_keys_for_profile(name: str) -> list[str]:
    """Best-effort extraction of env_key values referenced by a profile."""
    path = profile_path(name)
    if not path.is_file():
        return []
    try:
        import tomllib

        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception:
        return []
    keys: list[str] = []
    providers = data.get("model_providers")
    if isinstance(providers, dict):
        for provider in providers.values():
            if isinstance(provider, dict):
                env_key = provider.get("env_key")
                if isinstance(env_key, str) and env_key:
                    keys.append(env_key)
    return keys


def app_server_daemon_running(timeout: float = 15.0) -> bool:
    """Whether Codex's managed app-server daemon is currently running."""
    codex_bin = find_codex()
    if not codex_bin:
        return False
    try:
        proc = subprocess.run(
            [codex_bin, "app-server", "daemon", "version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return '"status":"running"' in (proc.stdout or "")


def restart_app_server_daemon(timeout: float = 30.0) -> tuple[bool, str]:
    """Restart the managed app-server daemon so it reloads config.toml.

    Codex caches the model catalog at startup; the daemon serves ``/model`` to
    the TUI, so a config change only takes effect after a restart. Returns
    ``(restarted, message)``; ``restarted=False`` when no daemon is running.
    """
    codex_bin = find_codex()
    if not codex_bin:
        return False, "未找到 codex"
    if not app_server_daemon_running():
        return False, "守护进程未运行"
    try:
        proc = subprocess.run(
            [codex_bin, "app-server", "daemon", "restart"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "已重启 app-server 守护进程"
    return False, (proc.stderr or proc.stdout or "重启失败").strip()


def official_models(timeout: float = 30.0) -> list[dict[str, object]]:
    """Read Codex's own model catalog via ``codex debug models``.

    Returns a list of ``{"slug", "display_name", "levels", "default"}`` dicts.
    Works without login (uses the built-in catalog).
    """
    codex_bin = find_codex()
    if not codex_bin:
        return []
    try:
        proc = subprocess.run(
            [codex_bin, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return []
    out: list[dict[str, object]] = []
    for model in data.get("models", []):
        if not isinstance(model, dict):
            continue
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        levels = [
            preset.get("effort")
            for preset in model.get("supported_reasoning_levels", [])
            if isinstance(preset, dict) and isinstance(preset.get("effort"), str)
        ]
        out.append(
            {
                "slug": slug,
                "display_name": str(model.get("display_name") or slug),
                "levels": levels,
                "default": model.get("default_reasoning_level"),
            }
        )
    return out
