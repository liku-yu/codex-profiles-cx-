"""Redaction helpers so API keys never show up in the UI or logs.

Codex provider configs may carry a literal key in ``experimental_bearer_token``
(and env-var based providers keep the real key in the environment). Anything cx
*displays* passes through here first; the on-disk config keeps the real value
because Codex needs it, but those files are chmod 600.
"""

from __future__ import annotations

import re

# Config keys whose values are secrets.
SECRET_KEYS = (
    "experimental_bearer_token",
    "api_key",
    "apikey",
    "bearer_token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
)

_TOML_SECRET_RE = re.compile(
    r'(?P<key>' + "|".join(SECRET_KEYS) + r')(?P<sep>\s*=\s*)"(?P<val>[^"]*)"',
    re.IGNORECASE,
)
_INLINE_SECRET_RE = re.compile(
    r"(?P<key>" + "|".join(SECRET_KEYS) + r")(?P<sep>\s*[:=]\s*)(?P<val>\S+)",
    re.IGNORECASE,
)
_SK_RE = re.compile(r"\b(sk-[A-Za-z0-9._\-]{6,})\b")


def mask(value: str) -> str:
    """Mask a secret, keeping a short prefix/suffix for recognition."""
    value = str(value)
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}…{value[-4:]}"


def redact_toml(text: str) -> str:
    """Mask secret values in a TOML snippet (keeps keys, hides values)."""
    return _TOML_SECRET_RE.sub(
        lambda m: f'{m.group("key")}{m.group("sep")}"{mask(m.group("val"))}"', text
    )


def redact_text(text: str) -> str:
    """Best-effort masking for free-form text (logs, error messages)."""
    text = _TOML_SECRET_RE.sub(
        lambda m: f'{m.group("key")}{m.group("sep")}"{mask(m.group("val"))}"', text
    )
    text = _INLINE_SECRET_RE.sub(
        lambda m: f'{m.group("key")}{m.group("sep")}{mask(m.group("val"))}', text
    )
    return _SK_RE.sub(lambda m: mask(m.group(1)), text)
