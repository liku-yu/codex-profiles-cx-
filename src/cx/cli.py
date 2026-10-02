"""Entry point for codex-profiles.

The tool is TUI-only: running ``cx`` opens the interactive interface.
"""

from cx.tui import run_tui


def main() -> None:
    run_tui()


if __name__ == "__main__":
    main()
