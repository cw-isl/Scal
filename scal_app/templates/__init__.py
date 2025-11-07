"""Template loaders for the smart frame web UI."""
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BOARD_TEMPLATE_PATH = PACKAGE_DIR / "board.html"
SETTINGS_TEMPLATE_PATH = PACKAGE_DIR / "settings.html"


def load_board_html() -> str:
    """Return the HTML content for the board page."""
    return BOARD_TEMPLATE_PATH.read_text(encoding="utf-8")


def load_settings_html() -> str:
    """Return the HTML content for the settings page."""
    return SETTINGS_TEMPLATE_PATH.read_text(encoding="utf-8")
