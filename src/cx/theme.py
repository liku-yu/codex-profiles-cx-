"""A Textual theme that matches the Codex CLI's look.

Colors are taken from Codex's own TUI style module (``tui/src/style.rs``):
the shared UI accent is ChatGPT Blue 200 (``#63A8F8``), with ChatGPT Blue 100
(``#A4CDFB``) for emphasis and a muted amber for warnings.
"""

from __future__ import annotations

from textual.theme import Theme

CODEX_THEME = Theme(
    name="codex",
    dark=True,
    primary="#63A8F8",       # ChatGPT Blue 200 (Codex UI accent)
    secondary="#A4CDFB",     # ChatGPT Blue 100
    accent="#5FAFFF",        # Codex syntax accent
    foreground="#E6EAF0",
    background="#0B0F14",
    surface="#151B22",
    panel="#1B232C",
    success="#3FB950",
    warning="#C4A767",       # Codex muted amber
    error="#F85149",
    variables={},
)
