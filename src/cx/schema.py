"""Schema validation plus Codex-specific sanity checks.

The bundled ``config.schema.json`` is the exact schema Codex generates from its
``ConfigToml`` type. A profile-v2 file is simply another config layer, so it is
validated against the same top level schema.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from cx.codex import REMOVED_PROVIDER_IDS, RESERVED_PROVIDER_IDS

_SCHEMA_PATH = Path(__file__).parent / "data" / "config.schema.json"

# Values Codex errors on with a migration message.
_REMOVED_WIRE_APIS = {"chat"}
_VALID_WIRE_APIS = {"responses"}


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    with _SCHEMA_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def validate_document(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    problems: list[str] = []
    problems.extend(_validate_with_jsonschema(data))
    problems.extend(_codex_specific_checks(data))
    return problems


def _validate_with_jsonschema(data: dict[str, Any]) -> list[str]:
    try:
        import jsonschema

        schema = load_schema()
        validator_cls = jsonschema.validators.validator_for(schema)
        validator = validator_cls(schema)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    except Exception as exc:  # pragma: no cover - defensive
        return [f"schema validation could not run: {exc}"]

    problems: list[str] = []
    for err in errors:
        location = ".".join(str(p) for p in err.path) or "<root>"
        problems.append(f"{location}: {err.message}")
    return problems


def _codex_specific_checks(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []

    if "profile" in data:
        problems.append(
            "top-level `profile = ...` is rejected by current Codex; "
            "profile-v2 files must not set it (remove this key)"
        )

    providers = data.get("model_providers")
    if isinstance(providers, dict):
        for pid, provider in providers.items():
            if pid in RESERVED_PROVIDER_IDS:
                problems.append(
                    f"model_providers.{pid}: `{pid}` is reserved by Codex and cannot be overridden"
                )
            if pid in REMOVED_PROVIDER_IDS:
                problems.append(
                    f"model_providers.{pid}: `{pid}` was removed by Codex; use `ollama` instead"
                )
            if isinstance(provider, dict):
                pname = provider.get("name")
                if not isinstance(pname, str) or not pname.strip():
                    problems.append(
                        f"model_providers.{pid}.name: Codex requires a non-empty provider name"
                    )
                if provider.get("env_key") and provider.get("experimental_bearer_token"):
                    problems.append(
                        f"model_providers.{pid}: `env_key` 与 `experimental_bearer_token` 同时设置时，"
                        "Codex 会用 `env_key`（变量缺失就报错），请只保留一个"
                    )
                wire = provider.get("wire_api")
                if isinstance(wire, str):
                    if wire in _REMOVED_WIRE_APIS:
                        problems.append(
                            f"model_providers.{pid}.wire_api: `chat` is no longer supported; "
                            "use `responses`"
                        )
                    elif wire not in _VALID_WIRE_APIS:
                        problems.append(
                            f"model_providers.{pid}.wire_api: `{wire}` is not valid; "
                            "use `responses`"
                        )

    model_provider = data.get("model_provider")
    if isinstance(model_provider, str) and model_provider:
        if model_provider in REMOVED_PROVIDER_IDS:
            problems.append(
                f"model_provider: `{model_provider}` was removed by Codex; use `ollama` instead"
            )

    return problems


def provider_reference_issues(
    data: dict[str, Any], base: dict[str, Any] | None
) -> list[str]:
    """Check that model_provider references a provider Codex can resolve."""
    provider = data.get("model_provider")
    if not isinstance(provider, str) or not provider:
        return []
    known = set(RESERVED_PROVIDER_IDS)
    for source in (base, data):
        if isinstance(source, dict):
            providers = source.get("model_providers")
            if isinstance(providers, dict):
                known.update(str(key) for key in providers)
    if provider not in known:
        return [
            (
                f"model_provider: `{provider}` is not defined in this profile or the base "
                "config.toml and is not a built-in provider"
            )
        ]
    return []


def base_config_issues(data: dict[str, Any], profile_names: list[str]) -> list[str]:
    """Problems in the user's base ~/.codex/config.toml that would break
    profile-v2 selection. Read-only; we only report them.
    """
    issues: list[str] = []
    if "profile" in data:
        issues.append(
            "base config.toml contains legacy `profile = ...`, which current Codex "
            "rejects. Remove it; use `cx run <name>` / `codex --profile <name>` instead."
        )
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        for name in profile_names:
            if name in profiles:
                issues.append(
                    f"base config.toml contains legacy `[profiles.{name}]`, which conflicts "
                    f"with the profile-v2 file `{name}.config.toml`. Move those settings into "
                    f"the profile file and delete the `[profiles.{name}]` table."
                )
    return issues
